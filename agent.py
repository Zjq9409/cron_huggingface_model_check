import os
import re
import math
import datetime
import html
from pathlib import Path

import requests
from huggingface_hub import HfApi

# ==================== CONFIGURATION ====================
# Leave empty to disable org filtering.
TARGET_ORGS = [
    "deepseek-ai", "Qwen", "meta-llama", "google",
    "microsoft", "THUDM","MiniMaxAI", "moonshotai", "zai-org"
]
MIN_DOWNLOADS = 100
# Brand-new releases have few downloads yet, so likes act as an alternate signal.
MIN_LIKES = 50
DAYS_BACK = 30
# Generative LLM pipelines, including multimodal variants such as VLMs.
TARGET_PIPELINES = {
    "text-generation",
    "text2text-generation",
    "image-text-to-text",
    "video-text-to-text",
    "audio-text-to-text",
    "any-to-any",
    "text-to-video",
    "image-to-video",
    "image-text-to-video",
}

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK")

OUTPUT_FILE = os.getenv("REPORT_PATH", "daily_hardware_report.md")
PAGES_DIR = os.getenv("PAGES_DIR", "docs")
# =======================================================

# Matches "-7B", "_1.5b", "-1.6T" but not "-7Bit".
PARAM_SIZE_RE = re.compile(r"[-_]([0-9]+(?:\.[0-9]+)?)\s*([BbTt])(?![a-zA-Z])")
TAG_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)[Bb]$")
# Vendors label activated MoE size in IDs such as "Qwen3-235B-A22B".
ACTIVATED_SIZE_RE = re.compile(r"[-_]A([0-9]+(?:\.[0-9]+)?)\s*([BbTt])(?![a-zA-Z])")

BYTES_PER_PARAM = {"FP16": 2.0, "INT8": 1.0, "INT4": 0.5}
RUNTIME_OVERHEAD = 1.20

# Ordered by usable VRAM so the smallest sufficient card wins.
INTEL_GPUS = [
    ("Intel Arc Pro B60 (24GB)", 24),
    ("Intel Arc Pro B70 (32GB)", 32),
    ("Intel CRI (128GB)", 128),
    ("Intel CRI (256GB)", 256),
]
NVIDIA_GPUS = [
    ("RTX 4090 (48GB)", 48),
    ("RTX PRO 5000 (72GB)", 72),
    ("H100 (80GB)", 80),
    ("RTX PRO 6000 (96GB)", 96),
    ("H200 (141GB)", 141),
    ("B200 (192GB)", 192),
    ("B300 (288GB)", 288),
]


class LLMHardwareAdvisorAgent:
    def __init__(self):
        self.api = HfApi()

    def fetch_recent_models(self):
        print("Polling Hugging Face Hub for new models...")
        now = datetime.datetime.now(datetime.timezone.utc)
        start_time = now - datetime.timedelta(days=DAYS_BACK)

        if TARGET_ORGS:
            models = (
                model
                for org in TARGET_ORGS
                for model in self.api.list_models(
                    author=org,
                    sort="lastModified",
                    limit=50,
                    full=True,
                )
            )
        else:
            models = self.api.list_models(
                sort="lastModified",
                limit=150,
                full=True,
            )

        filtered_models = []
        for model in models:
            if model.last_modified and model.last_modified < start_time:
                continue

            downloads = getattr(model, "downloads", 0) or 0
            likes = getattr(model, "likes", 0) or 0
            if downloads < MIN_DOWNLOADS and likes < MIN_LIKES:
                continue

            org = model.id.split("/")[0]
            if TARGET_ORGS and org not in TARGET_ORGS:
                continue

            tags = getattr(model, "tags", []) or []
            pipeline_tag = getattr(model, "pipeline_tag", "")
            if pipeline_tag not in TARGET_PIPELINES and not TARGET_PIPELINES.intersection(tags):
                continue

            filtered_models.append({
                "id": model.id,
                "author": org,
                "downloads": downloads,
                "likes": likes,
                "tags": tags,
                "pipeline_tag": pipeline_tag or "unknown",
                "last_modified": model.last_modified.strftime("%Y-%m-%d %H:%M:%S"),
            })

        filtered_models.sort(key=lambda item: item["last_modified"], reverse=True)
        print(f"Matched {len(filtered_models)} model(s).")
        return filtered_models

    def fetch_safetensors_metadata(self, model_id):
        """Returns (param_billions, published dtypes) from Hub safetensors metadata."""
        try:
            info = self.api.model_info(model_id, expand=["safetensors"])
        except Exception as exc:
            print(f"Could not read safetensors metadata for {model_id}: {exc}")
            return None, None

        safetensors = getattr(info, "safetensors", None)
        total = getattr(safetensors, "total", None)
        parameters = getattr(safetensors, "parameters", None) or {}

        dtypes = None
        if total and parameters:
            ranked = sorted(parameters.items(), key=lambda item: item[1], reverse=True)
            dtypes = ", ".join(f"{name} {count / total * 100:.1f}%" for name, count in ranked if count)

        return (total / 1e9 if total else None), dtypes

    def fetch_model_config(self, model_id):
        try:
            response = requests.get(
                f"https://huggingface.co/{model_id}/resolve/main/config.json",
                timeout=15,
            )
        except requests.RequestException as exc:
            print(f"Could not read config.json for {model_id}: {exc}")
            return None

        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def has_expert_fields(self, node):
        if isinstance(node, dict):
            return any(
                "expert" in key.lower() or "moe" in key.lower() or self.has_expert_fields(value)
                for key, value in node.items()
            )
        if isinstance(node, list):
            return any(self.has_expert_fields(item) for item in node)
        return False

    def flatten_config(self, config):
        """Multimodal repos nest the language model settings one level down."""
        inner = config.get("text_config") or config.get("llm_config")
        return {**config, **inner} if isinstance(inner, dict) else config

    def detect_architecture_type(self, model_id):
        """Expert fields only appear in the full config.json, not the Hub summary."""
        config = self.fetch_model_config(model_id)
        if config is None:
            return "Unknown", None
        if not self.has_expert_fields(config):
            return "Dense", None
        return "MoE", self.flatten_config(config)

    def describe_experts(self, moe_config):
        experts = moe_config.get("n_routed_experts") or moe_config.get("num_experts")
        top_k = moe_config.get("num_experts_per_tok") or moe_config.get("num_experts_per_token")
        return f"top-{top_k} of {experts}" if experts and top_k else None

    def estimate_attention_params(self, moe_config, hidden):
        heads = moe_config.get("num_attention_heads")
        if not heads:
            return 0

        # DeepSeek-style MLA compresses Q/KV through low-rank projections.
        q_lora = moe_config.get("q_lora_rank")
        kv_lora = moe_config.get("kv_lora_rank")
        if q_lora or kv_lora:
            nope = moe_config.get("qk_nope_head_dim") or 0
            rope = moe_config.get("qk_rope_head_dim") or 0
            v_dim = moe_config.get("v_head_dim") or 0
            q_lora = q_lora or 0
            kv_lora = kv_lora or 0
            query = hidden * q_lora + q_lora * heads * (nope + rope) if q_lora else hidden * heads * (nope + rope)
            key_value = hidden * (kv_lora + rope) + kv_lora * heads * (nope + v_dim)
            return query + key_value + heads * v_dim * hidden

        kv_heads = moe_config.get("num_key_value_heads") or heads
        head_dim = moe_config.get("head_dim") or (hidden // heads)
        return hidden * heads * head_dim * 2 + hidden * kv_heads * head_dim * 2

    def estimate_activated_params(self, model_id, moe_config):
        """Vendor-labelled activated size wins; config math is only an estimate."""
        named = ACTIVATED_SIZE_RE.search(model_id)
        if named:
            value = float(named.group(1))
            return (value * 1000.0 if named.group(2).lower() == "t" else value), False

        experts = moe_config.get("n_routed_experts") or moe_config.get("num_experts")
        top_k = moe_config.get("num_experts_per_tok") or moe_config.get("num_experts_per_token")
        layers = moe_config.get("num_hidden_layers")
        hidden = moe_config.get("hidden_size")
        expert_width = moe_config.get("moe_intermediate_size")
        vocab = moe_config.get("vocab_size")
        if not all([experts, top_k, layers, hidden, expert_width, vocab]):
            return None, False

        dense_layers = moe_config.get("first_k_dense_replace") or 0
        moe_layers = layers - dense_layers

        params = vocab * hidden * (1 if moe_config.get("tie_word_embeddings") else 2)
        params += self.estimate_attention_params(moe_config, hidden) * layers

        shared_count = moe_config.get("n_shared_experts") or moe_config.get("num_shared_experts") or 0
        shared_width = moe_config.get("shared_expert_intermediate_size") or expert_width * shared_count
        params += moe_layers * 3 * hidden * shared_width
        params += dense_layers * 3 * hidden * (moe_config.get("intermediate_size") or 0)
        params += moe_layers * hidden * experts
        params += moe_layers * top_k * 3 * hidden * expert_width

        activated = params / 1e9
        return (activated, True) if activated > 0 else (None, False)

    def resolve_model_specs(self, model_id, tags):
        """Hub metadata is authoritative; model naming is only a fallback."""
        param_size, dtypes = self.fetch_safetensors_metadata(model_id)
        return param_size or self.parse_parameter_size(model_id, tags), dtypes

    def parse_parameter_size(self, model_id, tags):
        """Returns parameter count in billions, or None if undetectable."""
        match = PARAM_SIZE_RE.search(model_id)
        if match:
            value = float(match.group(1))
            return value * 1000.0 if match.group(2).lower() == "t" else value

        for tag in tags:
            tag_match = TAG_SIZE_RE.match(tag)
            if tag_match:
                return float(tag_match.group(1))

        return None

    def calculate_vram_and_gpu(self, param_size):
        if not param_size:
            return None

        results = {}
        for precision, bytes_per_param in BYTES_PER_PARAM.items():
            weight_vram = param_size * bytes_per_param
            total_vram = weight_vram * RUNTIME_OVERHEAD
            results[precision] = {
                "weight": round(weight_vram, 2),
                "total": round(total_vram, 2),
                "intel": self.get_gpu_recommendations(total_vram, INTEL_GPUS),
                "nvidia": self.get_gpu_recommendations(total_vram, NVIDIA_GPUS),
            }
        return results

    def get_gpu_recommendations(self, vram_gb, options):
        for name, capacity in options:
            if vram_gb <= capacity:
                return f"1x {name}"

        largest_name, largest_capacity = options[-1]
        return f"{math.ceil(vram_gb / largest_capacity)}x {largest_name}"

    def generate_report(self, analyzed_models):
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        report = f"# Daily LLM Hardware Advisor Report ({today_str})\n\n"
        report += "*Automated collection of recently published/updated models, structural analysis, and resource recommendations.*\n\n"

        if not analyzed_models:
            report += f"No new models matching the target criteria were released in the past {DAYS_BACK} days.\n"
            return report

        for model in analyzed_models:
            report += f"## [{model['id']}](https://huggingface.co/{model['id']})\n"
            report += f"- **Creator/Org:** `{model['author']}` | **Task:** `{model['pipeline_tag']}`\n"
            report += f"- **Downloads (Last 30 Days):** `{model['downloads']:,}` | **Likes:** `{model['likes']:,}`\n"
            report += f"- **Last Updated:** `{model['last_modified']}`\n"
            param_size = model.get("param_size")
            param_text = f"{param_size:,.2f} B" if param_size else "unknown"
            report += f"- **Parameters:** `{param_text}` | **Published Dtypes:** `{model.get('dtypes') or 'unknown'}`\n"

            arch_text = model.get("arch_type", "Unknown")
            if model.get("experts"):
                arch_text += f" ({model['experts']} experts)"
            report += f"- **Architecture:** `{arch_text}`"
            activated = model.get("activated")
            if activated:
                prefix = "~" if model.get("activated_is_estimate") else ""
                report += f" | **Activated Params:** `{prefix}{activated:,.2f} B`"
            report += "\n"

            analysis = model["analysis"]
            if analysis:
                report += "\n#### VRAM Requirements & Recommended Hardware:\n"
                report += "| Precision / Quantization | Weight Size | Recommended VRAM | Intel Recommendation | NVIDIA Recommendation |\n"
                report += "| :--- | :--- | :--- | :--- | :--- |\n"
                for precision in ("FP16", "INT8", "INT4"):
                    data = analysis[precision]
                    report += f"| **{precision}** | {data['weight']} GB | **{data['total']} GB** | {data['intel']} | {data['nvidia']} |\n"
            else:
                report += "\n*Could not auto-detect parameter size from tags or model ID structure. Review model card manually.*\n"

            report += "\n---\n"

        return report

    def render_inline_markdown(self, text):
        escaped = html.escape(text)
        escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
        return escaped

    def markdown_to_html(self, markdown):
        html_parts = []
        lines = markdown.splitlines()
        index = 0

        while index < len(lines):
            line = lines[index].strip()

            if not line:
                index += 1
                continue

            if line == "---":
                html_parts.append("<hr>")
                index += 1
                continue

            if line.startswith("| ") and line.endswith("|"):
                rows = []
                while index < len(lines) and lines[index].strip().startswith("|"):
                    cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                    if not all(set(cell) <= {":", "-", " "} for cell in cells):
                        rows.append(cells)
                    index += 1

                if rows:
                    html_parts.append("<div class=\"table-wrap\"><table>")
                    headers = rows[0]
                    html_parts.append("<thead><tr>" + "".join(f"<th>{self.render_inline_markdown(cell)}</th>" for cell in headers) + "</tr></thead>")
                    if len(rows) > 1:
                        html_parts.append("<tbody>")
                        for row in rows[1:]:
                            html_parts.append("<tr>" + "".join(f"<td>{self.render_inline_markdown(cell)}</td>" for cell in row) + "</tr>")
                        html_parts.append("</tbody>")
                    html_parts.append("</table></div>")
                continue

            if line.startswith("#### "):
                html_parts.append(f"<h4>{self.render_inline_markdown(line[5:])}</h4>")
            elif line.startswith("### "):
                html_parts.append(f"<h3>{self.render_inline_markdown(line[4:])}</h3>")
            elif line.startswith("## "):
                html_parts.append(f"<h2>{self.render_inline_markdown(line[3:])}</h2>")
            elif line.startswith("# "):
                html_parts.append(f"<h1>{self.render_inline_markdown(line[2:])}</h1>")
            elif line.startswith("- "):
                html_parts.append(f"<ul><li>{self.render_inline_markdown(line[2:])}</li></ul>")
            else:
                html_parts.append(f"<p>{self.render_inline_markdown(line)}</p>")

            index += 1

        return "\n".join(html_parts).replace("</ul>\n<ul>", "")

    def generate_pages_site(self, report):
        pages_dir = Path(PAGES_DIR)
        pages_dir.mkdir(parents=True, exist_ok=True)
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        rendered_report = self.markdown_to_html(report)
        page = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Daily LLM Hardware Advisor</title>
  <style>
    :root {{ color-scheme: light dark; --bg: #0f172a; --card: #111827; --text: #e5e7eb; --muted: #9ca3af; --accent: #60a5fa; --border: #374151; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: linear-gradient(135deg, #020617, #1e293b); color: var(--text); }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 40px 20px 80px; }}
    .hero {{ padding: 28px; border: 1px solid var(--border); border-radius: 18px; background: rgba(17, 24, 39, 0.86); box-shadow: 0 20px 50px rgba(0,0,0,.28); }}
    .hero p {{ color: var(--muted); }}
    .report {{ margin-top: 24px; padding: 28px; border: 1px solid var(--border); border-radius: 18px; background: rgba(15, 23, 42, 0.92); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    h1, h2, h3, h4 {{ line-height: 1.25; }}
    h1 {{ margin-top: 0; }}
    h2 {{ margin-top: 34px; padding-top: 12px; border-top: 1px solid var(--border); }}
    code {{ padding: 2px 6px; border-radius: 6px; background: rgba(148, 163, 184, .16); }}
    ul {{ margin: 8px 0 14px 20px; padding: 0; }}
    li {{ margin: 6px 0; }}
    .table-wrap {{ overflow-x: auto; margin: 18px 0; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
    th, td {{ padding: 11px 12px; border: 1px solid var(--border); vertical-align: top; }}
    th {{ background: rgba(96, 165, 250, .14); text-align: left; }}
    hr {{ border: 0; border-top: 1px solid var(--border); margin: 28px 0; }}
    .footer {{ margin-top: 22px; color: var(--muted); font-size: 14px; }}
  </style>
</head>
<body>
  <main>
    <section class=\"hero\">
      <h1>Daily LLM Hardware Advisor</h1>
      <p>Latest automated Hugging Face model scan and hardware recommendation report.</p>
      <p><strong>Generated:</strong> {today_str}</p>
    </section>
    <section class=\"report\">
      {rendered_report}
    </section>
    <p class=\"footer\">Generated by GitHub Actions. Source Markdown: <a href=\"daily_hardware_report.md\">daily_hardware_report.md</a></p>
  </main>
</body>
</html>
"""
        (pages_dir / "index.html").write_text(page, encoding="utf-8")
        (pages_dir / "daily_hardware_report.md").write_text(report, encoding="utf-8")
        print(f"Generated GitHub Pages site: {pages_dir / 'index.html'}")

    def dispatch_webhooks(self, content):
        targets = [
            ("Feishu", FEISHU_WEBHOOK_URL, {"msg_type": "markdown", "content": {"text": content}}),
            ("Slack", SLACK_WEBHOOK_URL, {"text": content}),
        ]
        for name, url, payload in targets:
            if not url:
                continue
            try:
                response = requests.post(url, json=payload, timeout=10)
                response.raise_for_status()
                print(f"Successfully dispatched report to {name}.")
            except requests.RequestException as exc:
                print(f"Failed to send {name} webhook: {exc}")

    def run(self):
        models = self.fetch_recent_models()
        for model in models:
            param_size, dtypes = self.resolve_model_specs(model["id"], model["tags"])
            model["param_size"] = param_size
            model["dtypes"] = dtypes
            arch_type, moe_config = self.detect_architecture_type(model["id"])
            model["arch_type"] = arch_type
            model["experts"] = self.describe_experts(moe_config) if moe_config else None
            model["activated"], model["activated_is_estimate"] = (
                self.estimate_activated_params(model["id"], moe_config)
                if moe_config else (None, False)
            )
            model["analysis"] = self.calculate_vram_and_gpu(param_size)

        report = self.generate_report(models)

        with open(OUTPUT_FILE, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"Generated local markdown report: {OUTPUT_FILE}")

        self.generate_pages_site(report)

        self.dispatch_webhooks(report)


if __name__ == "__main__":
    LLMHardwareAdvisorAgent().run()
