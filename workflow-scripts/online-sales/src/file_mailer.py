from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import logging
import os
from pathlib import Path
import re
import shutil
import smtplib
import sys
import tempfile
from typing import Any

from config import FileMailerConfig
from storage import append_jsonl, ensure_parent, read_jsonl


@dataclass(frozen=True)
class FulfillmentResult:
    sale: dict[str, Any]
    status: str
    reason: str
    product_path: str = ""
    attachment_name: str = ""

    def to_record(self) -> dict[str, Any]:
        record = dict(self.sale)
        record["status"] = self.status
        record["fulfillment_reason"] = self.reason
        record["fulfilled_at"] = datetime.now(timezone.utc).isoformat()
        if self.product_path:
            record["product_path"] = self.product_path
        if self.attachment_name:
            record["attachment_name"] = self.attachment_name
        return record


def setup_logging(log_path: Path) -> None:
    ensure_parent(log_path)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_input_sale(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.order_json:
        payload = json.loads(args.order_json)
    elif env_payload := _workflow_input_from_env():
        payload = env_payload
    elif not sys.stdin.isatty():
        raw_input = sys.stdin.read().strip()
        if not raw_input:
            return None
        if raw_input.startswith("PAYMENT "):
            raw_input = raw_input[len("PAYMENT ") :]
        payload = json.loads(raw_input)
    else:
        return None

    if isinstance(payload, dict) and isinstance(payload.get("input"), dict):
        payload = payload["input"]

    if not isinstance(payload, dict):
        raise RuntimeError("Fulfillment input must be a JSON object")
    return payload


def _workflow_input_from_env() -> dict[str, Any] | None:
    for name in (
        "WORKFLOW_INPUT",
        "CODEX_WORKFLOW_INPUT",
        "NODE_INPUT",
        "INPUT",
        "input",
    ):
        value = os.getenv(name, "").strip()
        if not value:
            continue
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def verified_sales_from_file(path: Path) -> list[dict[str, Any]]:
    return [record for record in read_jsonl(path) if record.get("status") == "payment_verified"]


def is_no_new_sales(payload: Any) -> bool:
    if isinstance(payload, str):
        return payload.strip().lower() == "no new sales"
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    message = str(payload.get("message") or payload.get("result") or "").strip().lower()
    return status == "no_new_sales" or message == "no new sales"


def record_key(record: dict[str, Any]) -> str:
    for field in ("payment_reference", "order_id", "source_email_id", "message_id"):
        value = str(record.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return json.dumps(record, sort_keys=True)


def existing_record_keys(*paths: Path) -> set[str]:
    keys: set[str] = set()
    for path in paths:
        for record in read_jsonl(path):
            keys.add(record_key(record))
    return keys


def find_product_path(product: str, config: FileMailerConfig) -> Path | None:
    mapped = config.product_file_map.get(product)
    if mapped:
        mapped_path = Path(mapped)
        path = mapped_path if mapped_path.is_absolute() else config.product_root_dir / mapped_path
        return path if path.exists() else None

    if not config.product_root_dir.exists():
        return None

    product_key = normalize_product_name(product)
    for candidate in config.product_root_dir.iterdir():
        if normalize_product_name(candidate.stem) == product_key or normalize_product_name(candidate.name) == product_key:
            return candidate
    return None


def normalize_product_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def prepare_attachment(product_path: Path, temp_dir: Path) -> Path:
    if product_path.is_file():
        return product_path
    if not product_path.is_dir():
        raise RuntimeError(f"Product path is not a file or directory: {product_path}")

    archive_base = temp_dir / product_path.name
    archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=product_path)
    return Path(archive_path)


def build_message(sale: dict[str, Any], config: FileMailerConfig, attachment_path: Path) -> EmailMessage:
    buyer_email = str(sale.get("buyer_email") or "").strip()
    buyer_name = str(sale.get("buyer_name") or "").strip()
    product = str(sale.get("product") or "").strip()
    greeting = f"Hi {buyer_name}," if buyer_name else "Hi,"

    message = EmailMessage()
    message["From"] = config.smtp_from
    message["To"] = buyer_email
    message["Subject"] = f"Your {product} purchase"
    message.set_content(
        "\n".join(
            [
                greeting,
                "",
                f"Thanks for your purchase. Your {product} file is attached.",
                "",
                "If you have any trouble opening it, reply to this email and I will help.",
                "",
                "Josh",
            ]
        )
    )

    data = attachment_path.read_bytes()
    message.add_attachment(
        data,
        maintype="application",
        subtype="octet-stream",
        filename=attachment_path.name,
    )
    return message


def send_message(message: EmailMessage, config: FileMailerConfig) -> None:
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        if config.smtp_use_tls:
            smtp.starttls()
        smtp.login(config.smtp_username, config.smtp_password)
        smtp.send_message(message)


def fulfill_sale(sale: dict[str, Any], config: FileMailerConfig, dry_run: bool) -> FulfillmentResult:
    if sale.get("status") != "payment_verified":
        return FulfillmentResult(sale, "fulfillment_review_required", "payment_not_verified")

    buyer_email = str(sale.get("buyer_email") or "").strip()
    product = str(sale.get("product") or "").strip()
    if not buyer_email:
        return FulfillmentResult(sale, "fulfillment_review_required", "missing_buyer_email")
    if not product:
        return FulfillmentResult(sale, "fulfillment_review_required", "missing_product")

    product_path = find_product_path(product, config)
    if not product_path:
        return FulfillmentResult(
            sale,
            "fulfillment_review_required",
            "product_file_not_found",
        )

    with tempfile.TemporaryDirectory(prefix="sales-fulfillment-") as temp_name:
        attachment_path = prepare_attachment(product_path, Path(temp_name))
        message = build_message(sale, config, attachment_path)
        if dry_run:
            logging.info(
                "DRY RUN would send %s to %s for product %s.",
                attachment_path,
                buyer_email,
                product,
            )
        else:
            send_message(message, config)

        return FulfillmentResult(
            sale,
            "fulfilled",
            "email_sent" if not dry_run else "dry_run_email_not_sent",
            str(product_path),
            attachment_path.name,
        )


def workflow_fulfillment_result(result: FulfillmentResult) -> dict[str, Any]:
    return {
        "buyer_email": result.sale.get("buyer_email", ""),
        "buyer_name": result.sale.get("buyer_name", ""),
        "order_id": result.sale.get("order_id", ""),
        "payment_reference": result.sale.get("payment_reference", ""),
        "product": result.sale.get("product", ""),
        "status": result.status,
        "reason": result.reason,
        "product_path": result.product_path,
        "attachment_name": result.attachment_name,
    }


def print_result(result: FulfillmentResult) -> None:
    print("FULFILLMENT " + json.dumps(workflow_fulfillment_result(result), sort_keys=True), flush=True)


def print_workflow_result(payload: object) -> None:
    print("@@WORKFLOW_RESULT@@" + json.dumps(payload, sort_keys=True), flush=True)


def run(args: argparse.Namespace, config: FileMailerConfig) -> int:
    dry_run = args.dry_run or config.fulfillment_test_mode
    sale = load_input_sale(args)
    if is_no_new_sales(sale):
        logging.info("No new sales from payment verifier; skipping fulfillment.")
        print_workflow_result({"status": "no_new_sales", "message": "No New Sales"})
        return 0

    sales = [sale] if sale else verified_sales_from_file(config.verified_sales_output_path) if args.from_file else []
    if not sales:
        logging.info("No verified sales to fulfill.")
        print_workflow_result({"status": "no_new_sales", "message": "No New Sales"})
        return 0

    fulfilled_count = 0
    workflow_results: list[dict[str, Any]] = []
    seen_output_keys = existing_record_keys(
        config.fulfilled_sales_output_path,
        config.fulfillment_review_output_path,
        config.fulfillment_log_path,
    )
    for sale_record in sales:
        key = record_key(sale_record)
        if key in seen_output_keys:
            logging.info("Skipped duplicate fulfillment output for %s.", key)
            continue

        result = fulfill_sale(sale_record, config, dry_run)
        output_path = (
            config.fulfilled_sales_output_path
            if result.status == "fulfilled"
            else config.fulfillment_review_output_path
        )

        if not dry_run:
            append_jsonl(config.fulfillment_log_path, result.to_record())
            append_jsonl(output_path, result.to_record())

        seen_output_keys.add(key)
        print_result(result)
        workflow_results.append(workflow_fulfillment_result(result))
        if result.status == "fulfilled":
            fulfilled_count += 1

    if not workflow_results:
        print_workflow_result({"status": "already_fulfilled", "message": "Order already fulfilled"})
        return 0

    print_workflow_result(workflow_results[0] if len(workflow_results) == 1 else workflow_results)
    if fulfilled_count != len(workflow_results):
        raise SystemExit(1)
    return fulfilled_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Email purchased files after payment verification.")
    parser.add_argument("--dry-run", action="store_true", help="Prepare fulfillment without sending email.")
    parser.add_argument("--order-json", help="Single verified order JSON object to fulfill.")
    parser.add_argument(
        "--from-file",
        action="store_true",
        help="Read verified sales from VERIFIED_SALES_OUTPUT_PATH when no single order input is provided.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = FileMailerConfig.from_env()
    setup_logging(config.file_mailer_log_path)

    if config.fulfillment_test_mode:
        logging.info("FULFILLMENT_TEST_MODE is enabled: emails will not be sent.")

    count = run(args, config)
    logging.info("Fulfilled %s sale orders.", count)


if __name__ == "__main__":
    main()
