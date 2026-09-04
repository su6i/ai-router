"""Cross-model comparison over the delegation ledger (D-233).

The ledger already proves a run *finished* — verify_status, attempts,
self_fix_rounds. None of that measures whether the code was any good, so a
reviewer's verdict is recorded here as its own record type and folded into
the same table as the machine-collected numbers.

`cost_usd_equiv` in those records is what a call WOULD have cost on the paid
API while it actually rode a $0 subscription channel. It is a comparison
number only: never billed, never counted against a budget cap.
"""
import datetime
import json
import os
from pathlib import Path

QUALITY_MIN = 1
QUALITY_MAX = 5


def record_review_score(audit_path, model: str, quality: int, note: str,
                         task: str = "") -> dict:
    """Append one reviewer verdict to the ledger and return the record.

    Raises ValueError when quality is outside QUALITY_MIN..QUALITY_MAX.
    """
    if isinstance(quality, bool) or not isinstance(quality, int) or not (QUALITY_MIN <= quality <= QUALITY_MAX):
        raise ValueError(f"quality must be an integer between {QUALITY_MIN} and {QUALITY_MAX} (1-5)")

    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    rec = {
        "ts": ts,
        "mode": "review",
        "model_asked": model,
        "quality": quality,
        "note": note,
        "task": task,
        # Who stands behind this number. A verdict is the only subjective field
        # in the ledger, so it must be attributable: on 2026-09-04 a worker with
        # shell access wrote a 4/5 for a model and task nobody had reviewed,
        # and an unsigned row is indistinguishable from a fabricated one.
        "by": os.environ.get("AI_ROUTER_REVIEWER", "unattributed"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def show_scorecard(audit_path, since: str | None = None) -> str:
    """Return the per-model comparison table as plain text.

    Groups every ledger record by model_asked. Skips malformed lines rather
    than failing the report, and returns a one-line message when the ledger
    does not exist. A column with no data for a model shows "-", never a
    fabricated 0. `since` filters on the record's YYYY-MM-DD ts prefix.
    """
    path = Path(audit_path)
    if not path.exists():
        return "(no audit.log yet)"

    model_stats = {}
    reviews_by_model = {}

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                rec = json.loads(line_str)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue

            model = rec.get("model_asked") or rec.get("model")
            if not model:
                continue
            model = str(model)

            day_str = str(rec.get("ts", ""))[:10]
            if since and day_str < since:
                continue

            if rec.get("mode") == "review":
                quality_val = rec.get("quality")
                if (
                    isinstance(quality_val, (int, float))
                    and not isinstance(quality_val, bool)
                    and QUALITY_MIN <= quality_val <= QUALITY_MAX
                ):
                    reviews_by_model.setdefault(model, []).append(quality_val)
                continue

            stats = model_stats.setdefault(model, {
                "runs": 0,
                "verify_pass": 0,
                "verify_total": 0,
                "attempts_sum": 0.0,
                "attempts_count": 0,
                "self_fix_rounds_total": 0,
                "self_fix_rounds_triggered": 0,
                "latency_sum": 0.0,
                "latency_count": 0,
                "has_in_tokens": False,
                "in_tokens": 0,
                "has_out_tokens": False,
                "out_tokens": 0,
                "has_cost_usd": False,
                "cost_usd_sum": 0.0,
                "has_cost_usd_equiv": False,
                "cost_usd_equiv_sum": 0.0,
            })
            stats["runs"] += 1

            v_status = rec.get("verify_status")
            if isinstance(v_status, str):
                v_status_upper = v_status.upper()
                if v_status_upper in ("PASS", "FAIL"):
                    stats["verify_total"] += 1
                    if v_status_upper == "PASS":
                        stats["verify_pass"] += 1

            att = rec.get("attempts")
            if att is not None and isinstance(att, (int, float)) and not isinstance(att, bool):
                stats["attempts_sum"] += att
                stats["attempts_count"] += 1

            sfr = rec.get("self_fix_rounds")
            if sfr is not None and isinstance(sfr, (int, float)) and not isinstance(sfr, bool):
                stats["self_fix_rounds_total"] += 1
                if sfr > 0:
                    stats["self_fix_rounds_triggered"] += 1

            lat = rec.get("latency_s")
            if lat is not None and isinstance(lat, (int, float)) and not isinstance(lat, bool):
                stats["latency_sum"] += lat
                stats["latency_count"] += 1

            in_tok = rec.get("in")
            if in_tok is not None and isinstance(in_tok, (int, float)) and not isinstance(in_tok, bool):
                stats["has_in_tokens"] = True
                stats["in_tokens"] += in_tok

            out_tok = rec.get("out")
            if out_tok is not None and isinstance(out_tok, (int, float)) and not isinstance(out_tok, bool):
                stats["has_out_tokens"] = True
                stats["out_tokens"] += out_tok

            cost = rec.get("cost_usd")
            if cost is not None and isinstance(cost, (int, float)) and not isinstance(cost, bool):
                stats["has_cost_usd"] = True
                stats["cost_usd_sum"] += cost

            eq = rec.get("cost_usd_equiv")
            if eq is not None and isinstance(eq, (int, float)) and not isinstance(eq, bool):
                stats["has_cost_usd_equiv"] = True
                stats["cost_usd_equiv_sum"] += eq

    all_models = set(m for m, s in model_stats.items() if s["runs"] > 0) | set(
        m for m, q in reviews_by_model.items() if len(q) > 0
    )
    if not all_models:
        return "(no scorecard data yet)"

    headers = [
        "model", "runs", "verify_pass", "avg_attempts", "self_fix_rate",
        "avg_latency_s", "in_tokens", "out_tokens", "real_usd", "equiv_usd", "quality"
    ]
    rows = []
    for m in sorted(all_models):
        s = model_stats.get(m, {
            "runs": 0,
            "verify_pass": 0,
            "verify_total": 0,
            "attempts_sum": 0.0,
            "attempts_count": 0,
            "self_fix_rounds_total": 0,
            "self_fix_rounds_triggered": 0,
            "latency_sum": 0.0,
            "latency_count": 0,
            "has_in_tokens": False,
            "in_tokens": 0,
            "has_out_tokens": False,
            "out_tokens": 0,
            "has_cost_usd": False,
            "cost_usd_sum": 0.0,
            "has_cost_usd_equiv": False,
            "cost_usd_equiv_sum": 0.0,
        })
        q_list = reviews_by_model.get(m, [])

        col_model = m
        col_runs = str(s["runs"])
        col_verify = f"{(s['verify_pass'] / s['verify_total'] * 100):.1f}%" if s["verify_total"] > 0 else "-"
        col_attempts = f"{(s['attempts_sum'] / s['attempts_count']):.2f}" if s["attempts_count"] > 0 else "-"
        col_self_fix = (
            f"{(s['self_fix_rounds_triggered'] / s['self_fix_rounds_total'] * 100):.1f}%"
            if s["self_fix_rounds_total"] > 0
            else "-"
        )
        col_latency = f"{(s['latency_sum'] / s['latency_count']):.2f}" if s["latency_count"] > 0 else "-"
        col_in = str(int(round(s["in_tokens"]))) if s["has_in_tokens"] else "-"
        col_out = str(int(round(s["out_tokens"]))) if s["has_out_tokens"] else "-"
        col_real = f"{s['cost_usd_sum']:.6f}" if s["has_cost_usd"] else "-"
        col_equiv = f"{s['cost_usd_equiv_sum']:.6f}" if s["has_cost_usd_equiv"] else "-"
        col_quality = f"{(sum(q_list) / len(q_list)):.2f} (n={len(q_list)})" if q_list else "-"

        rows.append([
            col_model, col_runs, col_verify, col_attempts, col_self_fix,
            col_latency, col_in, col_out, col_real, col_equiv, col_quality
        ])

    all_rows = [headers] + rows
    col_widths = [max(len(r[i]) for r in all_rows) for i in range(len(headers))]

    table_lines = [
        "  ".join(headers[i].ljust(col_widths[i]) for i in range(len(headers))),
        "  ".join("-" * col_widths[i] for i in range(len(headers))),
    ]
    for row in rows:
        table_lines.append("  ".join(row[i].ljust(col_widths[i]) for i in range(len(headers))))

    return "\n".join(table_lines)
