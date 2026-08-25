import os
import re
import datetime

import requests
from huggingface_hub import HfApi

# ==================== CONFIGURATION ====================
# Leave empty to disable org filtering.
TARGET_ORGS = [
    "deepseek-ai", "Qwen", "meta-llama", "mistralai", "google",
    "microsoft", "01-ai", "internlm", "baichuan-inc", "THUDM",
]
MIN_DOWNLOADS = 100
DAYS_BACK = 1

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK")

OUTPUT_FILE = os.getenv("REPORT_PATH", "daily_hardware_report.md")
# =======================================================

# Matches "-7B", "_1.5b", "-1.6T" but not "-7Bit".
PARAM_SIZE_RE = re.compile(r"[-_]([0-9]+(?:\.[0-9]+)?)\s*([BbTt])(?![a-zA-Z])")
TAG_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)[Bb]$")

BYTES_PER_PARAM = {"FP16": 2.0, "INT8": 1.0, "INT4": 0.5}
RUNTIME_OVERHEAD = 1.20


class LLMHardwareAdvisorAgent:
    def __init__(self):
        self.api = HfApi()

    def fetch_recent_models(self):
        print("Polling Hugging Face Hub for new models...")
        now = datetime.datetime.now(datetime.timezone.utc)
        start_time = now - datetime.timedelta(days=DAYS_BACK)

        models = self.api.list_models(
            sort="lastModified",
            direction=-1,
            limit=150,
            full=True,
        )

        filtered_models = []
        for model in models:
            if model.last_modified and model.last_modified < start_time:
                continue

            downloads = getattr(model, "downloads", 0) or 0
            if downloads < MIN_DOWNLOADS:
                continue

            org = model.id.split("/")[0]
            if TARGET_ORGS and org not in TARGET_ORGS:
                continue

            tags = getattr(model, "tags", []) or []
            pipeline_tag = getattr(model, "pipeline_tag", "")
            if pipeline_tag != "text-generation" and "text-generation" not in tags:
                continue

            filtered_models.append({
                "id": model.id,
                "author": org,
                "downloads": downloads,
                "likes": getattr(model, "likes", 0) or 0,
                "tags": tags,
                "last_modified": model.last_modified.strftime("%Y-%m-%d %H:%M:%S"),
            })

        print(f"Matched {len(filtered_models)} model(s).")
        return filtered_models

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
                "gpus": self.get_gpu_recommendations(total_vram),
            }
        return results

    def get_gpu_recommendations(self, vram_gb):
        if vram_gb < 16:
            return "Intel Arc B580 (12GB) / RTX 4060 Ti (16GB) / RTX 4080"
        if vram_gb < 24:
            return "Intel Arc B70 (24GB) / RTX 3090 / RTX 4090 (24GB)"
        if vram_gb < 48:
            return "1x RTX 6000 Ada (48GB) / 2x RTX 4090 (48GB)"
        if vram_gb < 80:
            return "1x NVIDIA A100 (80GB) / 1x Intel Gaudi 3 (96GB) / 3x RTX 4090"
        if vram_gb < 160:
            return "2x NVIDIA A100/H100 (80GB) / 2x Intel Gaudi 3 (96GB)"
        if vram_gb < 320:
            return "4x H100 (80GB) / 4x Intel Gaudi 3 (96GB)"
        if vram_gb < 640:
            return "8x H100 (80GB) / 1 Node H200 (141GB)"
        nodes = int(vram_gb // 640) + 1
        return f"Multi-node GPU Cluster (Recommend {nodes}x Node of 8x H200 141GB or B200)"

    def generate_report(self, analyzed_models):
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        report = f"# Daily LLM Hardware Advisor Report ({today_str})\n\n"
        report += "*Automated collection of recently published/updated models, structural analysis, and resource recommendations.*\n\n"

        if not analyzed_models:
            report += "No new models matching the target criteria were released in the past 24 hours.\n"
            return report

        for model in analyzed_models:
            report += f"## [{model['id']}](https://huggingface.co/{model['id']})\n"
            report += f"- **Creator/Org:** `{model['author']}`\n"
            report += f"- **Downloads (Last 30 Days):** `{model['downloads']:,}` | **Likes:** `{model['likes']:,}`\n"
            report += f"- **Last Updated:** `{model['last_modified']}`\n"

            analysis = model["analysis"]
            if analysis:
                report += "\n#### VRAM Requirements & Recommended Hardware:\n"
                report += "| Precision / Quantization | Weight Size | Recommended VRAM | Recommended Hardware Setup |\n"
                report += "| :--- | :--- | :--- | :--- |\n"
                for precision in ("FP16", "INT8", "INT4"):
                    data = analysis[precision]
                    report += f"| **{precision}** | {data['weight']} GB | **{data['total']} GB** | {data['gpus']} |\n"
            else:
                report += "\n*Could not auto-detect parameter size from tags or model ID structure. Review model card manually.*\n"

            report += "\n---\n"

        return report

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
            param_size = self.parse_parameter_size(model["id"], model["tags"])
            model["analysis"] = self.calculate_vram_and_gpu(param_size)

        report = self.generate_report(models)

        with open(OUTPUT_FILE, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"Generated local markdown report: {OUTPUT_FILE}")

        self.dispatch_webhooks(report)


if __name__ == "__main__":
    LLMHardwareAdvisorAgent().run()
