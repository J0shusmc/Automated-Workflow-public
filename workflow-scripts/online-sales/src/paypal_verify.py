from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import PayPalVerifyConfig
from storage import append_jsonl, ensure_parent, read_json, read_jsonl


COMPLETED_TRANSACTION_STATUSES = {"S", "COMPLETED", "COMPLETED_FUNDS_HELD"}
LEGACY_PAYPAL_API_BASE_URL = "https://api.paypal.com"


@dataclass(frozen=True)
class VerificationResult:
    sale: dict[str, Any]
    status: str
    reason: str
    paypal_transaction: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        record = dict(self.sale)
        record["status"] = public_payment_status(self.status)
        record["payment_verification_reason"] = self.reason
        record["payment_verified_at"] = datetime.now(timezone.utc).isoformat()
        transaction_info = (self.paypal_transaction or {}).get("transaction_info", {})
        if isinstance(transaction_info, dict):
            record["paypal_transaction_id"] = transaction_info.get("transaction_id", "")
            record["paypal_reference_id"] = transaction_info.get("paypal_reference_id", "")
        return record


def public_payment_status(status: str) -> str:
    return "payment_verified" if status == "payment_verified" else "no_payment_received"


def workflow_payment_result(result: VerificationResult) -> dict[str, str]:
    return {
        "payment_verified": public_payment_status(result.status),
        "product": str(result.sale.get("product") or ""),
        "buyer_email": str(result.sale.get("buyer_email") or ""),
    }


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


class PayPalClient:
    def __init__(self, config: PayPalVerifyConfig) -> None:
        self.config = config
        self._access_token: str | None = None

    def access_token(self) -> str:
        if self._access_token:
            return self._access_token

        credentials = f"{self.config.paypal_client_id}:{self.config.paypal_client_secret}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        response = self._request(
            "POST",
            "/v1/oauth2/token",
            headers={
                "Accept": "application/json",
                "Accept-Language": "en_US",
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=urlencode({"grant_type": "client_credentials"}).encode("utf-8"),
            auth=False,
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("PayPal OAuth response did not include access_token")

        self._access_token = token
        return token

    def transaction_search(
        self,
        *,
        start_date: datetime,
        end_date: datetime,
        transaction_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "start_date": start_date.isoformat(timespec="seconds"),
            "end_date": end_date.isoformat(timespec="seconds"),
            "fields": "all",
        }
        if transaction_id:
            params["transaction_id"] = transaction_id
        query = urlencode(params)
        path = f"/v1/reporting/transactions?{query}"
        try:
            response = self._request(
                "GET",
                path,
                headers={"PayPal-Enforce-ISO8601-Format": "true"},
            )
        except RuntimeError as error:
            if (
                str(error) != "not_authorized"
                or self.config.paypal_api_base_url == LEGACY_PAYPAL_API_BASE_URL
            ):
                raise
            logging.info("Retrying PayPal Transaction Search with legacy API host.")
            response = self._request(
                "GET",
                path,
                headers={"PayPal-Enforce-ISO8601-Format": "true"},
                base_url=LEGACY_PAYPAL_API_BASE_URL,
            )
        transactions = response.get("transaction_details", [])
        if not isinstance(transactions, list):
            return []
        return [item for item in transactions if isinstance(item, dict)]

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        auth: bool = True,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if auth:
            request_headers["Authorization"] = f"Bearer {self.access_token()}"

        request = Request(
            f"{base_url or self.config.paypal_api_base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )

        try:
            with urlopen(request, timeout=30) as response:
                raw_body = response.read().decode("utf-8")
        except HTTPError as error:
            raw_error = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(_paypal_error_reason(error.code, raw_error)) from error
        except URLError as error:
            raise RuntimeError(f"PayPal API {method} {path} failed: {error}") from error

        return json.loads(raw_body) if raw_body else {}


def verify_sale(
    sale: dict[str, Any],
    client: PayPalClient,
    config: PayPalVerifyConfig,
) -> VerificationResult:
    payment_reference = str(sale.get("payment_reference") or sale.get("order_id") or "").strip()

    received_at = _parse_datetime(str(sale.get("received_at") or ""))
    if received_at:
        start_date = received_at - timedelta(days=config.payment_verify_window_days)
        end_date = received_at + timedelta(days=config.payment_verify_window_days)
    else:
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=config.payment_verify_lookback_days)

    try:
        if payment_reference:
            transactions = client.transaction_search(
                transaction_id=payment_reference,
                start_date=start_date,
                end_date=end_date,
            )
        else:
            transactions = []
        if not transactions:
            logging.info("No exact PayPal transaction ID match; searching by sale details.")
            transactions = client.transaction_search(start_date=start_date, end_date=end_date)
    except RuntimeError as error:
        logging.error(
            "PayPal verification failed for payment reference %s: %s",
            payment_reference,
            error,
        )
        return VerificationResult(
            sale,
            "payment_review_required",
            f"paypal_api_error_{_safe_reason(str(error))}",
        )
    expected_amount = _expected_amount(sale, config)
    if expected_amount is None:
        return VerificationResult(sale, "payment_review_required", "missing_expected_amount")

    if not transactions:
        return VerificationResult(sale, "payment_review_required", "paypal_transaction_not_found")

    transaction = _best_transaction_match(
        transactions,
        payment_reference,
        sale,
        expected_amount,
        config,
        received_at,
    )
    if not transaction:
        return VerificationResult(sale, "payment_review_required", "paypal_transaction_not_found")

    transaction_info = transaction.get("transaction_info", {})
    payer_info = transaction.get("payer_info", {})
    status = str(transaction_info.get("transaction_status", "")).upper()

    if status not in COMPLETED_TRANSACTION_STATUSES:
        return VerificationResult(
            sale,
            "payment_review_required",
            f"paypal_transaction_status_{status or 'missing'}",
            transaction,
        )

    actual_amount = _money_value(transaction_info.get("transaction_amount", {}))
    if actual_amount is None or actual_amount != expected_amount:
        return VerificationResult(
            sale,
            "payment_review_required",
            f"amount_mismatch_expected_{expected_amount}_actual_{actual_amount}",
            transaction,
        )

    expected_currency = config.payment_verify_currency
    actual_currency = str(transaction_info.get("transaction_amount", {}).get("currency_code", "")).upper()
    if actual_currency and actual_currency != expected_currency:
        return VerificationResult(
            sale,
            "payment_review_required",
            f"currency_mismatch_expected_{expected_currency}_actual_{actual_currency}",
            transaction,
        )

    sale_email = str(sale.get("buyer_email", "")).strip().lower()
    payer_email = str(payer_info.get("email_address", "")).strip().lower()
    if sale_email and payer_email and sale_email != payer_email:
        return VerificationResult(
            sale,
            "payment_review_required",
            "buyer_email_mismatch",
            transaction,
        )

    return VerificationResult(sale, "payment_verified", "paypal_payment_verified", transaction)


def _best_transaction_match(
    transactions: list[dict[str, Any]],
    payment_reference: str,
    sale: dict[str, Any],
    expected_amount: Decimal,
    config: PayPalVerifyConfig,
    received_at: datetime | None,
) -> dict[str, Any] | None:
    sale_email = str(sale.get("buyer_email", "")).strip().lower()
    buyer_name = str(sale.get("buyer_name", "")).strip().lower()
    for transaction in transactions:
        if payment_reference and payment_reference in _transaction_reference_values(transaction):
            return transaction

    matches = [
        transaction
        for transaction in transactions
        if _transaction_matches_sale(transaction, sale_email, buyer_name, expected_amount, config)
    ]
    if len(matches) == 1:
        return matches[0]
    if received_at and len(matches) > 1:
        close_matches = _closest_transactions(matches, received_at, config)
        if len(close_matches) == 1:
            return close_matches[0]
    if len(matches) > 1:
        logging.warning("Found %s PayPal sale-detail matches; manual review required.", len(matches))
    return None


def _transaction_reference_values(transaction: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for section_name in ("transaction_info", "cart_info"):
        section = transaction.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for value in section.values():
            if isinstance(value, str):
                values.add(value.strip())

    cart_info = transaction.get("cart_info", {})
    if isinstance(cart_info, dict):
        for item in cart_info.get("item_details", []):
            if not isinstance(item, dict):
                continue
            for value in item.values():
                if isinstance(value, str):
                    values.add(value.strip())

    return {value for value in values if value}


def _transaction_matches_sale(
    transaction: dict[str, Any],
    sale_email: str,
    buyer_name: str,
    expected_amount: Decimal,
    config: PayPalVerifyConfig,
) -> bool:
    transaction_info = transaction.get("transaction_info", {})
    payer_info = transaction.get("payer_info", {})
    if not isinstance(transaction_info, dict) or not isinstance(payer_info, dict):
        return False

    if str(transaction_info.get("transaction_status", "")).upper() not in COMPLETED_TRANSACTION_STATUSES:
        return False

    if _money_value(transaction_info.get("transaction_amount", {})) != expected_amount:
        return False

    actual_currency = str(transaction_info.get("transaction_amount", {}).get("currency_code", "")).upper()
    if actual_currency and actual_currency != config.payment_verify_currency:
        return False

    payer_email = str(payer_info.get("email_address", "")).strip().lower()
    if sale_email and payer_email:
        return sale_email == payer_email

    payer_name = _payer_name(payer_info).lower()
    return bool(buyer_name and payer_name and buyer_name in payer_name)


def _closest_transactions(
    transactions: list[dict[str, Any]],
    received_at: datetime,
    config: PayPalVerifyConfig,
) -> list[dict[str, Any]]:
    max_delta = timedelta(hours=config.payment_verify_fallback_window_hours)
    scored: list[tuple[timedelta, dict[str, Any]]] = []
    for transaction in transactions:
        transaction_info = transaction.get("transaction_info", {})
        if not isinstance(transaction_info, dict):
            continue
        transaction_at = _parse_datetime(
            str(
                transaction_info.get("transaction_initiation_date")
                or transaction_info.get("transaction_updated_date")
                or ""
            )
        )
        if not transaction_at:
            continue
        delta = abs(transaction_at - received_at)
        if delta <= max_delta:
            scored.append((delta, transaction))

    if not scored:
        return []
    scored.sort(key=lambda item: item[0])
    closest_delta = scored[0][0]
    return [transaction for delta, transaction in scored if delta == closest_delta]


def _payer_name(payer_info: dict[str, Any]) -> str:
    payer_name = payer_info.get("payer_name", {})
    if not isinstance(payer_name, dict):
        return ""
    parts = [
        str(payer_name.get("given_name", "")).strip(),
        str(payer_name.get("surname", "")).strip(),
        str(payer_name.get("alternate_full_name", "")).strip(),
    ]
    return " ".join(part for part in parts if part)


def _expected_amount(sale: dict[str, Any], config: PayPalVerifyConfig) -> Decimal | None:
    amount = _decimal_from_amount(str(sale.get("amount") or ""))
    if amount is not None:
        return amount

    product = str(sale.get("product") or "")
    mapped_amount = config.product_price_map.get(product)
    if mapped_amount is None:
        return None
    return _decimal_from_amount(str(mapped_amount))


def _money_value(value: dict[str, Any]) -> Decimal | None:
    if not isinstance(value, dict):
        return None
    return _decimal_from_amount(str(value.get("value") or ""))


def _decimal_from_amount(value: str) -> Decimal | None:
    cleaned = value.replace(",", "").strip()
    if not cleaned:
        return None
    cleaned = "".join(char for char in cleaned if char.isdigit() or char in ".-")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_reason(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "_" for char in value)
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:160] or "unknown"


def _paypal_error_reason(status_code: int, raw_error: str) -> str:
    try:
        payload = json.loads(raw_error)
    except json.JSONDecodeError:
        return f"http_{status_code}"

    if isinstance(payload, dict):
        if payload.get("error"):
            return str(payload["error"]).lower()
        for detail in payload.get("details", []):
            if isinstance(detail, dict) and detail.get("issue"):
                return str(detail["issue"]).lower()
        if payload.get("name"):
            return str(payload["name"]).lower()

    return f"http_{status_code}"


def load_input_sale(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.order_json:
        payload = json.loads(args.order_json)
    elif env_payload := _workflow_input_from_env():
        payload = env_payload
    elif not sys.stdin.isatty():
        raw_input = sys.stdin.read().strip()
        if not raw_input:
            return None
        if raw_input.startswith("ORDER "):
            raw_input = raw_input[len("ORDER ") :]
        payload = json.loads(raw_input)
    else:
        return None

    if isinstance(payload, dict) and isinstance(payload.get("input"), dict):
        payload = payload["input"]

    if not isinstance(payload, dict):
        raise RuntimeError("Order input must be a JSON object")
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


def pending_sales_from_file(path: Path) -> list[dict[str, Any]]:
    return [
        record
        for record in read_jsonl(path)
        if record.get("status") in {"pending_payment_verification", "no_payment_received"}
    ]


def latest_sale_from_file(path: Path) -> dict[str, Any] | None:
    payload = read_json(path)
    if not isinstance(payload, dict):
        return None
    if payload.get("status") and payload.get("status") not in {
        "pending_payment_verification",
        "no_payment_received",
    }:
        return None
    return payload


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


def print_result(result: VerificationResult) -> None:
    print("PAYMENT " + json.dumps(workflow_payment_result(result), sort_keys=True), flush=True)


def print_workflow_result(payload: object) -> None:
    print("@@WORKFLOW_RESULT@@" + json.dumps(payload, sort_keys=True), flush=True)


def run(args: argparse.Namespace, config: PayPalVerifyConfig) -> int:
    dry_run = args.dry_run or config.payment_verify_test_mode
    sale = load_input_sale(args)
    if is_no_new_sales(sale):
        logging.info("No new sales from mail reader; skipping PayPal verification.")
        print_workflow_result({"status": "no_new_sales", "message": "No New Sales"})
        return 0

    latest_sale = latest_sale_from_file(config.latest_sale_path) if not sale and not args.from_file else None
    if is_no_new_sales(latest_sale):
        logging.info("No new sales from latest sale file; skipping PayPal verification.")
        print_workflow_result({"status": "no_new_sales", "message": "No New Sales"})
        return 0

    sales = [sale] if sale else [latest_sale] if latest_sale else pending_sales_from_file(config.sales_output_path) if args.from_file else []
    if not sales:
        logging.info("No pending sales to verify.")
        print_workflow_result({"status": "no_new_sales", "message": "No New Sales"})
        return 0

    client = PayPalClient(config)
    verified_count = 0
    workflow_results: list[dict[str, Any]] = []
    seen_output_keys = existing_record_keys(
        config.verified_sales_output_path,
        config.payment_review_output_path,
    )
    for sale_record in sales:
        result = verify_sale(sale_record, client, config)
        output_path = (
            config.verified_sales_output_path
            if result.status == "payment_verified"
            else config.payment_review_output_path
        )

        if dry_run:
            logging.info("DRY RUN payment result: %s", result.to_record())
        elif record_key(result.sale) in seen_output_keys:
            logging.info("Skipped duplicate payment verification output for %s.", record_key(result.sale))
        else:
            append_jsonl(output_path, result.to_record())
            seen_output_keys.add(record_key(result.sale))

        print_result(result)
        workflow_results.append(workflow_payment_result(result))
        if result.status == "payment_verified":
            verified_count += 1

    print_workflow_result(workflow_results[0] if len(workflow_results) == 1 else workflow_results)
    if verified_count != len(workflow_results):
        raise SystemExit(1)
    return verified_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify pending sale payments against PayPal.")
    parser.add_argument("--dry-run", action="store_true", help="Verify and print results without writing output files.")
    parser.add_argument("--order-json", help="Single order JSON object to verify instead of reading SALES_OUTPUT_PATH.")
    parser.add_argument(
        "--from-file",
        action="store_true",
        help="Read pending sales from SALES_OUTPUT_PATH when no single order input is provided.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = PayPalVerifyConfig.from_env()
    setup_logging(config.payment_verifier_log_path)

    if config.payment_verify_test_mode:
        logging.info("PAYMENT_VERIFY_TEST_MODE is enabled: verification results will not be written.")

    count = run(args, config)
    logging.info("Verified %s sale payments.", count)


if __name__ == "__main__":
    main()
