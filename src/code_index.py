import argparse
import json
import sys
import os
import hashlib
import subprocess
import ast
from pathlib import Path
import psycopg

import tree_sitter_python as tspython
import tree_sitter_bash as tsbash
from tree_sitter import Language, Parser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegate import load_env, project_info
from rules_index import E5Model

def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS code_chunks (
                id bigserial PRIMARY KEY,
                repo text,
                path text,
                lang text,
                symbol text,
                parent_symbol text,
                start_line int,
                end_line int,
                chunk text,
                chunk_hash text,
                repo_commit text,
                embedding vector(384)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx 
            ON code_chunks USING hnsw (embedding vector_cosine_ops);
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS code_edges (
                caller_id bigint REFERENCES code_chunks(id) ON DELETE CASCADE,
                callee_symbol text,
                resolved_id bigint REFERENCES code_chunks(id) ON DELETE SET NULL,
                PRIMARY KEY (caller_id, callee_symbol)
            );
        """)
    conn.commit()

# Tree-sitter parsers
PY_LANG = Language(tspython.language())
BASH_LANG = Language(tsbash.language())

def get_parser(lang):
    p = Parser(lang)
    return p

def get_signature(node, source_bytes):
    # Extracts everything up to the body
    body = node.child_by_field_name('body')
    if not body:
        return source_bytes[node.start_byte:node.end_byte].decode('utf-8')
    sig_bytes = source_bytes[node.start_byte:body.start_byte]
    return sig_bytes.decode('utf-8').strip()

def chunk_node(root_node, lang, tokenizer, source_bytes, parent_symbol=None):
    # Iterative pre-order walk over `node.children` lists only. The recursive
    # cursor/next_sibling walk deterministically segfaulted py-tree-sitter
    # 0.26.0 on macOS with real-size files; materialized children are stable.
    chunks = []

    def emit(node, current_symbol, current_parent_symbol):
        text = source_bytes[node.start_byte:node.end_byte].decode('utf-8')
        tokens = len(text) // 3

        if tokens > 400:
            body = node.child_by_field_name('body')
            if body:
                sig = get_signature(node, source_bytes)
                body_children = body.children
                current_subchunk_text = ""
                current_start = -1

                for i, child in enumerate(body_children):
                    child_text = source_bytes[child.start_byte:child.end_byte].decode('utf-8')
                    temp = current_subchunk_text + "\n" + child_text if current_subchunk_text else child_text
                    if (len(sig) + len(temp)) // 3 > 400:
                        if current_subchunk_text:
                            prev_child = body_children[i - 1] if i > 0 else None
                            chunks.append({
                                "symbol": current_symbol,
                                "parent_symbol": current_parent_symbol,
                                "start_line": current_start + 1,
                                "end_line": prev_child.end_point.row + 1 if prev_child else current_start + 1,
                                "text": sig + "\n" + current_subchunk_text.strip()
                            })
                        current_subchunk_text = child_text
                        current_start = child.start_point.row
                    else:
                        if not current_subchunk_text:
                            current_start = child.start_point.row
                        current_subchunk_text = temp

                if current_subchunk_text:
                    chunks.append({
                        "symbol": current_symbol,
                        "parent_symbol": current_parent_symbol,
                        "start_line": current_start + 1,
                        "end_line": body_children[-1].end_point.row + 1 if body_children else current_start + 1,
                        "text": sig + "\n" + current_subchunk_text.strip()
                    })
                return

        chunks.append({
            "symbol": current_symbol,
            "parent_symbol": current_parent_symbol,
            "start_line": node.start_point.row + 1,
            "end_line": node.end_point.row + 1,
            "text": text
        })

    stack = [(root_node, parent_symbol)]
    while stack:
        node, current_parent_symbol = stack.pop()

        symbol_name = None
        if node.type in ('function_definition', 'class_definition'):
            name_node = node.child_by_field_name('name')
            if name_node:
                symbol_name = source_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8')

        current_symbol = symbol_name if symbol_name else current_parent_symbol
        if current_parent_symbol and symbol_name:
            current_symbol = f"{current_parent_symbol}.{symbol_name}"

        if node.type in ('function_definition', 'class_definition'):
            emit(node, current_symbol, current_parent_symbol)

        next_parent = current_symbol if node.type in ('class_definition', 'function_definition') else current_parent_symbol
        for child in reversed(node.children):
            stack.append((child, next_parent))

    return chunks

def cmd_chunk_files(paths):
    """Chunk the given files and print {path: [chunk, ...]} as JSON.

    Runs as a dedicated child process that only ever touches tree-sitter:
    live tokenizers/onnxruntime objects in the same process corrupt
    tree-sitter walks on macOS arm64 (deterministic segfault — see
    docs/CODE-RAG.md), so the child never creates them and the parent
    (embedding/DB side) never parses.
    """
    out = {}
    for rel in paths:
        lang = 'python' if rel.endswith('.py') else 'bash'
        try:
            source_bytes = Path(rel).read_bytes()
            parser = get_parser(PY_LANG if lang == 'python' else BASH_LANG)
            tree = parser.parse(source_bytes)
            chunks = [c for c in chunk_node(tree.root_node, lang, None, source_bytes) if c['text'].strip()]
        except Exception as e:
            print(f"Failed to process {rel}: {e}", file=sys.stderr)
            continue
        out[rel] = chunks
    print(json.dumps(out))


def _chunk_files_subprocess(paths):
    """Chunk files via one `chunk-files` child process; returns {path: chunks}."""
    if not paths:
        return {}
    root = Path(__file__).resolve().parent.parent
    res = subprocess.run(
        [sys.executable, "-m", "src.code_index", "chunk-files", *[str(p) for p in paths]],
        cwd=root, capture_output=True, text=True, check=True,
    )
    if res.stderr:
        print(res.stderr, file=sys.stderr, end="")
    return json.loads(res.stdout)


def extract_python_calls(source):
    calls = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return calls
    
    current_class = None
    current_func = None
    
    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            nonlocal current_class, current_func
            prev_class = current_class
            current_class = node.name
            self.generic_visit(node)
            current_class = prev_class

        def visit_FunctionDef(self, node):
            nonlocal current_class, current_func
            prev_func = current_func
            name = f"{current_class}.{node.name}" if current_class else node.name
            current_func = name
            self.generic_visit(node)
            current_func = prev_func
            
        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)

        def visit_Call(self, node):
            if current_func:
                if isinstance(node.func, ast.Name):
                    calls.add((current_func, node.func.id))
                elif isinstance(node.func, ast.Attribute):
                    calls.add((current_func, node.func.attr))
            self.generic_visit(node)
            
    Visitor().visit(tree)
    return calls

def cmd_reindex(args):
    load_env()
    repo_name, commit = project_info()
    if not repo_name:
        repo_name = "ai-router"
    if not commit:
        commit = "unknown"

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("Error: POSTGRES_DSN not set.", file=sys.stderr)
        sys.exit(1)
        
    repo_path = Path.cwd()
    
    with psycopg.connect(dsn) as conn:
        init_db(conn)
        
        indexed_commit = None
        if not args.rebuild:
            with conn.cursor() as cur:
                cur.execute("SELECT repo_commit FROM code_chunks WHERE repo = %s LIMIT 1", (repo_name,))
                row = cur.fetchone()
                if row:
                    indexed_commit = row[0]
                    
        target_files = []
        if indexed_commit and indexed_commit != "unknown" and indexed_commit != commit:
            try:
                res = subprocess.run(["git", "diff", "--name-only", f"{indexed_commit}..HEAD"],
                                     capture_output=True, text=True, check=True)
                changed_files = res.stdout.splitlines()
                target_files = [Path(f) for f in changed_files if Path(f).exists() and (f.endswith('.py') or f.endswith('.sh'))]
                # Vanished files
                vanished = [f for f in changed_files if not Path(f).exists()]
                if vanished:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM code_chunks WHERE repo = %s AND path = ANY(%s)", (repo_name, vanished))
            except subprocess.CalledProcessError:
                # fallback to all
                pass
        
        if not target_files and not (indexed_commit and not args.rebuild):
            # full rebuild / fallback
            target_files = []
            try:
                res = subprocess.run(["git", "ls-files", "--", "*.py", "*.sh"], cwd=repo_path, capture_output=True, text=True, check=True)
                target_files = [repo_path / f for f in res.stdout.splitlines() if (repo_path / f).exists()]
            except subprocess.CalledProcessError:
                pass
                
        if not target_files and not args.rebuild:
            # nothing changed
            # just update commit hash maybe?
            with conn.cursor() as cur:
                cur.execute("UPDATE code_chunks SET repo_commit = %s WHERE repo = %s", (commit, repo_name))
            conn.commit()
            return
            
        chunk_map = _chunk_files_subprocess([f.resolve() for f in target_files])
        model = E5Model()

        for filepath in target_files:
            try:
                rel_path = str(filepath.resolve().relative_to(repo_path.resolve()))
            except ValueError:
                rel_path = str(filepath)

            lang = 'python' if rel_path.endswith('.py') else 'bash'
            source = filepath.read_text('utf-8')
            chunks = chunk_map.get(str(filepath.resolve()))
            if chunks is None:
                continue

            current_shas = []
            
            print(f"File {filepath}: generated {len(chunks)} chunks", file=sys.stderr)
            sys.stderr.flush()
            
            chunk_records = []
            for c in chunks:
                chunk_text = c['text']
                chunk_sha = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                current_shas.append(chunk_sha)
                
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM code_chunks WHERE repo = %s AND path = %s AND chunk_hash = %s",
                        (repo_name, rel_path, chunk_sha)
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute("UPDATE code_chunks SET repo_commit = %s WHERE id = %s", (commit, row[0]))
                        chunk_records.append((row[0], c['symbol']))
                        continue
                        
                    try:
                        header = f"{lang} {c['symbol']} in {rel_path}"
                        emb = model.embed([chunk_text], prefix=f"{header}\npassage: ")[0].tolist()
                    except Exception as e:
                        print(f"Embedding failed: {e}", file=sys.stderr)
                        sys.stderr.flush()
                        raise
                    
                    cur.execute(
                        "INSERT INTO code_chunks (repo, path, lang, symbol, parent_symbol, start_line, end_line, chunk, chunk_hash, repo_commit, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (repo_name, rel_path, lang, c['symbol'], c['parent_symbol'], c['start_line'], c['end_line'], chunk_text, chunk_sha, commit, str(emb))
                    )
                    chunk_id = cur.fetchone()[0]
                    chunk_records.append((chunk_id, c['symbol']))

            # GC chunks no longer in this file
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM code_chunks WHERE repo = %s AND path = %s AND NOT (chunk_hash = ANY(%s))",
                    (repo_name, rel_path, current_shas)
                )

            # Update call graph for Python
            if lang == 'python':
                calls = extract_python_calls(source)
                with conn.cursor() as cur:
                    for cid, sym in chunk_records:
                        cur.execute("DELETE FROM code_edges WHERE caller_id = %s", (cid,))
                        for caller_sym, callee_sym in calls:
                            if caller_sym == sym:
                                cur.execute("INSERT INTO code_edges (caller_id, callee_symbol) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cid, callee_sym))

        # Resolve edges
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE code_edges ce
                SET resolved_id = cc.id
                FROM code_chunks cc
                WHERE ce.callee_symbol = cc.symbol AND cc.repo = %s
            """, (repo_name,))
            # GC vanished paths entirely when rebuild
            if args.rebuild:
                indexed_paths = [str(f.resolve().relative_to(repo_path.resolve())) for f in target_files]
                cur.execute("DELETE FROM code_chunks WHERE repo = %s AND NOT (path = ANY(%s))", (repo_name, indexed_paths))
            
            cur.execute("UPDATE code_chunks SET repo_commit = %s WHERE repo = %s", (commit, repo_name))

        conn.commit()

def cmd_search(args):
    load_env()
    repo_name, commit = project_info()
    if not repo_name:
        repo_name = "ai-router"
    
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("Error: POSTGRES_DSN not set.", file=sys.stderr)
        sys.exit(1)
        
    query = args.query
    k = args.k
    
    model = E5Model()
    q_emb = model.embed([query], prefix="query: ")[0].tolist()
    
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT repo_commit FROM code_chunks WHERE repo = %s LIMIT 1", (repo_name,))
            row = cur.fetchone()
            if row and commit and row[0] != commit:
                print(f"Warning: code index is stale. Index commit: {row[0]}, Current commit: {commit}", file=sys.stderr)
                
            repo_filter = ""
            params = [repo_name, str(q_emb), k]
            if args.repo:
                repo_filter = "AND path LIKE %s"
                params.insert(1, f"{args.repo}%")
                
            cur.execute(f"""
                SELECT id, path, start_line, end_line, symbol, chunk 
                FROM code_chunks 
                WHERE repo = %s {repo_filter}
                ORDER BY embedding <=> %s::vector 
                LIMIT %s
            """, params)
            
            results = cur.fetchall()
            
            if args.graph and results:
                hit_ids = [r[0] for r in results]
                # Fetch 1-hop callers
                cur.execute("""
                    SELECT cc.id, cc.path, cc.start_line, cc.end_line, cc.symbol, cc.chunk
                    FROM code_edges ce
                    JOIN code_chunks cc ON ce.caller_id = cc.id
                    WHERE ce.resolved_id = ANY(%s)
                """, (hit_ids,))
                callers = cur.fetchall()
                
                # Fetch 1-hop callees
                cur.execute("""
                    SELECT cc.id, cc.path, cc.start_line, cc.end_line, cc.symbol, cc.chunk
                    FROM code_edges ce
                    JOIN code_chunks cc ON ce.resolved_id = cc.id
                    WHERE ce.caller_id = ANY(%s)
                """, (hit_ids,))
                callees = cur.fetchall()
                
                all_res = {r[0]: r for r in results}
                for r in callers + callees:
                    if r[0] not in all_res:
                        all_res[r[0]] = r
                results = list(all_res.values())
            
    out = []
    total_chars = 0
    for r in results:
        path, start_line, end_line, symbol, chunk = r[1], r[2], r[3], r[4], r[5]
        s = symbol if symbol else "unknown"
        prefix = f"{path}:{start_line}-{end_line} [{s}]"
        item = f"{prefix}\n{chunk}\n"
        if total_chars + len(item) > 8000:  # ~2k tokens
            break
        out.append(item)
        total_chars += len(item)
        
    print("\n---\n".join(out))

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    
    p_reindex = subparsers.add_parser("reindex")
    p_reindex.add_argument("--rebuild", action="store_true")
    
    p_search = subparsers.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=5)
    p_search.add_argument("--graph", action="store_true")
    p_search.add_argument("--repo")
    
    p_chunk = subparsers.add_parser("chunk-files")
    p_chunk.add_argument("paths", nargs="*")

    args = parser.parse_args()
    try:
        if args.cmd == "reindex":
            cmd_reindex(args)
        elif args.cmd == "search":
            cmd_search(args)
        elif args.cmd == "chunk-files":
            cmd_chunk_files(args.paths)
    except psycopg.OperationalError:
        sys.exit("❌ Postgres not reachable — start it first: colima start")

if __name__ == "__main__":
    main()
