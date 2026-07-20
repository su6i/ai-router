import argparse
import sys
import os
import hashlib
from pathlib import Path
import psycopg
from tokenizers import Tokenizer
import onnxruntime as ort
import numpy as np
from huggingface_hub import hf_hub_download

# Import delegate under ONE module identity ("delegate"), whether we run as
# `python -m src.rules_index` (r.sh) or get imported by mcp/server.py, whose
# sys.path already carries src/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegate import load_env, project_info  # noqa: E402

_TOKENIZER = None


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        _TOKENIZER = Tokenizer.from_pretrained("intfloat/multilingual-e5-small")
    return _TOKENIZER

def mean_pooling(last_hidden_states, attention_mask):
    input_mask_expanded = np.expand_dims(attention_mask, -1)
    sum_embeddings = np.sum(last_hidden_states * input_mask_expanded, axis=1)
    sum_mask = np.clip(np.sum(input_mask_expanded, axis=1), a_min=1e-9, a_max=None)
    return sum_embeddings / sum_mask

class E5Model:
    def __init__(self):
        # We only download once (the only allowed network access). huggingface_hub caches it.
        # Ensure we suppress HF warnings or keep them visible.
        self.model_path = hf_hub_download(repo_id="intfloat/multilingual-e5-small", filename="onnx/model.onnx")
        self.tokenizer = Tokenizer.from_pretrained("intfloat/multilingual-e5-small")
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("<pad>"), pad_token="<pad>")
        self.session = ort.InferenceSession(self.model_path, providers=['CPUExecutionProvider'])
        
    def embed(self, texts, prefix="passage: "):
        formatted_texts = [prefix + t for t in texts]
        encoded = self.tokenizer.encode_batch(formatted_texts)
        
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        
        inputs = {'input_ids': input_ids, 'attention_mask': attention_mask}
        # e5-small might need token_type_ids if present in model
        input_names = [i.name for i in self.session.get_inputs()]
        if 'token_type_ids' in input_names:
            inputs['token_type_ids'] = np.zeros_like(input_ids)
            
        outputs = self.session.run(None, inputs)
        last_hidden_states = outputs[0]
        
        embeddings = mean_pooling(last_hidden_states, attention_mask)
        # L2 normalization for cosine similarity
        norm = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / np.clip(norm, a_min=1e-9, a_max=None)
        return embeddings

def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rules_chunks (
                id bigserial PRIMARY KEY,
                repo text,
                path text,
                heading text,
                start_line int,
                chunk text,
                chunk_sha text,
                repo_commit text,
                embedding vector(384)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS rules_chunks_embedding_idx 
            ON rules_chunks USING hnsw (embedding vector_cosine_ops);
        """)
    conn.commit()

def chunk_markdown(text, max_tokens=300):
    tokenizer = _get_tokenizer()

    lines = text.splitlines()
    chunks = []
    
    current_heading = ""
    current_chunk_lines = []
    current_start_line = 1
    
    def emit_chunk(current_line_idx):
        nonlocal current_chunk_lines, current_start_line
        if not current_chunk_lines:
            return
        
        chunk_text = "\n".join(current_chunk_lines).strip()
        # A chunk that is ONLY its heading (e.g. a title directly followed by
        # the next heading) or has no words at all (e.g. a lone `---` rule)
        # has no retrieval value and pollutes top-k.
        if chunk_text and chunk_text != current_heading \
                and any(c.isalnum() for c in chunk_text):
            chunks.append({
                "heading": current_heading,
                "start_line": current_start_line,
                "text": chunk_text
            })
        current_chunk_lines = []
        current_start_line = current_line_idx + 1

    in_fence = False
    for i, line in enumerate(lines):
        # `#` inside a ``` code fence is a comment, not a heading — without
        # this, bash comments in README examples become bogus chunks.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        # check if heading
        if not in_fence and line.startswith("#") and " " in line:
            parts = line.split(" ", 1)
            if all(c == "#" for c in parts[0]):
                emit_chunk(i)
                current_heading = line.strip()
                current_chunk_lines.append(line)
                current_start_line = i + 1
                continue
                
        # if paragraph break and chunk is big
        if not line.strip():
            # count tokens
            temp_text = "\n".join(current_chunk_lines)
            tokens = len(tokenizer.encode(temp_text).ids)
            if tokens > max_tokens:
                emit_chunk(i)
                continue
                
        current_chunk_lines.append(line)
        
    emit_chunk(len(lines))
    return chunks

def cmd_reindex(args):
    load_env()
    repo_name, commit = project_info()
    
    if not repo_name:
        repo_name = "ai-router"
        
    if not commit:
        # fallback for commit if not in git repo?
        commit = "unknown"

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("Error: POSTGRES_DSN not set.", file=sys.stderr)
        sys.exit(1)
        
    with psycopg.connect(dsn) as conn:
        init_db(conn)
        
        # files: .agent/constitution/rules/*.md, docs/**/*.md, CLAUDE.md
        repo_path = Path.cwd()
        target_files = []
        
        # Follow symlinks with resolve() or explicitly checking .agent/constitution
        constitution_dir = repo_path / ".agent" / "constitution"
        if constitution_dir.exists():
            rules_dir = constitution_dir / "rules"
            if rules_dir.exists():
                for md in rules_dir.glob("*.md"):
                    target_files.append(md)
        
        docs_dir = repo_path / "docs"
        if docs_dir.exists():
            for md in docs_dir.rglob("*.md"):
                # Translations (docs/fa, *.fa.md) duplicate the canonical
                # English content; indexing both drowns cross-lingual queries
                # (a Persian query then only ever hits the Persian mirror,
                # never the canonical rule text). e5 is multilingual: Persian
                # queries still match English chunks.
                if "fa" in md.relative_to(docs_dir).parts or md.name.endswith(".fa.md"):
                    continue
                target_files.append(md)
                
        claude_md = repo_path / "CLAUDE.md"
        if claude_md.exists():
            target_files.append(claude_md)
            
        model = E5Model()

        indexed_paths = []
        for filepath in target_files:
            try:
                rel_path = str(filepath.resolve().relative_to(repo_path.resolve()))
            except ValueError:
                rel_path = str(filepath)
            indexed_paths.append(rel_path)

            text = filepath.read_text()
            chunks = chunk_markdown(text)
            current_shas = []

            for chunk in chunks:
                chunk_text = chunk["text"]
                chunk_sha = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                current_shas.append(chunk_sha)
                
                # Check if exists
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, repo_commit FROM rules_chunks WHERE repo = %s AND path = %s AND chunk_sha = %s",
                        (repo_name, rel_path, chunk_sha)
                    )
                    row = cur.fetchone()
                    if row:
                        if row[1] != commit:
                            cur.execute(
                                "UPDATE rules_chunks SET repo_commit = %s WHERE id = %s",
                                (commit, row[0])
                            )
                        continue
                    
                    # Compute embedding
                    emb = model.embed([chunk_text], prefix="passage: ")[0].tolist()
                    
                    cur.execute(
                        "INSERT INTO rules_chunks (repo, path, heading, start_line, chunk, chunk_sha, repo_commit, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (repo_name, rel_path, chunk["heading"], chunk["start_line"], chunk_text, chunk_sha, commit, str(emb))
                    )

            # GC: drop chunks this file no longer contains (edited/deleted
            # paragraphs would otherwise stay and pollute retrieval forever).
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM rules_chunks WHERE repo = %s AND path = %s AND NOT (chunk_sha = ANY(%s))",
                    (repo_name, rel_path, current_shas)
                )

        # GC: drop paths that vanished from the corpus entirely.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM rules_chunks WHERE repo = %s AND NOT (path = ANY(%s))",
                (repo_name, indexed_paths)
            )
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
            # Check staleness
            cur.execute("SELECT repo_commit FROM rules_chunks WHERE repo = %s LIMIT 1", (repo_name,))
            row = cur.fetchone()
            if row and commit and row[0] != commit:
                print(f"Warning: rules index is stale. Index commit: {row[0]}, Current commit: {commit}", file=sys.stderr)
            
            # HNSW cosine distance (<=>)
            cur.execute("""
                SELECT path, start_line, heading, chunk 
                FROM rules_chunks 
                WHERE repo = %s
                ORDER BY embedding <=> %s::vector 
                LIMIT %s
            """, (repo_name, str(q_emb), k))
            
            results = cur.fetchall()
            
    # output cap enforcement ~8000 chars
    out = []
    total_chars = 0
    for path, start_line, heading, chunk in results:
        h = heading if heading else "No heading"
        prefix = f"{path}:{start_line} [{h}]"
        # ensure prefix is clear
        item = f"{prefix}\n{chunk}\n"
        if total_chars + len(item) > 8000:
            break
        out.append(item)
        total_chars += len(item)
        
    print("\n---\n".join(out))

def main():
    parser = argparse.ArgumentParser(description="Rules index and search")
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    
    subparsers.add_parser("reindex")
    
    parser_search = subparsers.add_parser("search")
    parser_search.add_argument("query", help="Search query")
    parser_search.add_argument("-k", type=int, default=5, help="Number of results")
    
    args = parser.parse_args()
    try:
        if args.cmd == "reindex":
            cmd_reindex(args)
        elif args.cmd == "search":
            cmd_search(args)
    except psycopg.OperationalError:
        sys.exit("❌ Postgres not reachable — start it first: colima start")

if __name__ == "__main__":
    main()
