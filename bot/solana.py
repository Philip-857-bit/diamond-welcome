"""Helius webhook management, mint validation, and buy-event parsing."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from bot.database import Database, WatchedToken

logger = logging.getLogger(__name__)

TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {WSOL_MINT: "SOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}
BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


class HeliusError(RuntimeError):
    """Safe, user-displayable Helius integration failure."""


@dataclass(frozen=True, slots=True)
class BuyAlert:
    token: WatchedToken
    buyer: str
    token_amount: Decimal
    payment_amount: Decimal | None
    payment_symbol: str | None
    signature: str


def is_public_key(value: str) -> bool:
    """Perform strict base58/32-byte validation without another dependency."""
    if not 32 <= len(value) <= 44 or any(char not in BASE58_ALPHABET for char in value):
        return False
    number = 0
    for char in value:
        number = (
            number * 58
            + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz".index(char)
        )
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    decoded = b"\0" * (len(value) - len(value.lstrip("1"))) + decoded
    return len(decoded) == 32


class HeliusClient:
    def __init__(
        self,
        api_key: str,
        webhook_secret: str,
        public_base_url: str,
        webhook_path: str,
    ) -> None:
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.webhook_url = f"{public_base_url.rstrip('/')}/{webhook_path.strip('/')}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))

    async def close(self) -> None:
        await self.http.aclose()

    async def _rpc_post_with_retry(
        self,
        rpc_url: str,
        payload: dict[str, Any],
        *,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """POST to Helius RPC with a short retry on 429."""
        method = payload.get("method", "unknown")
        for attempt in range(max_retries + 1):
            try:
                response = await self.http.post(rpc_url, json=payload)
            except httpx.HTTPError as exc:
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(2)
                continue

            if response.is_success:
                try:
                    return response.json()
                except ValueError as exc:
                    raise HeliusError(
                        "Solana returned a non-JSON response."
                    ) from exc

            if response.status_code != 429 or attempt >= max_retries:
                status = response.status_code
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if isinstance(body, dict) and body.get("error"):
                    code, message = self._safe_rpc_error(body["error"])
                    raise HeliusError(
                        f"RPC error [{code}]: {message} (HTTP {status})"
                    )
                raise HeliusError(
                    f"RPC request failed (HTTP {status})"
                )

            retry_header = response.headers.get("Retry-After")
            try:
                delay = max(2, int(retry_header)) if retry_header else 2
            except (TypeError, ValueError):
                delay = 2
            logger.warning("%s rate-limited (attempt %d/%d), retrying in %ds", method, attempt + 1, max_retries, delay)
            await asyncio.sleep(delay)

        raise HeliusError("RPC retry limit exhausted")

    def _safe_rpc_error(self, error: Any) -> tuple[str, str]:
        """Return bounded log fields without leaking credentials or control chars."""
        if not isinstance(error, dict):
            return "unknown", "No provider error details"

        def clean(value: Any, limit: int) -> str:
            text = " ".join(str(value).split()) if value is not None else "unknown"
            if self.api_key:
                text = text.replace(self.api_key, "[redacted]")
            return text[:limit]

        return clean(error.get("code"), 40), clean(error.get("message"), 300)

    async def validate_mint(self, mint: str, added_by: int) -> WatchedToken:
        if not is_public_key(mint):
            raise ValueError("That is not a valid Solana mint address.")

        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"

        account_payload = await self._rpc_post_with_retry(
            rpc_url,
            {
                "jsonrpc": "2.0",
                "id": "solducks-mint",
                "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}],
            },
        )
        result = account_payload.get("result") if isinstance(account_payload, dict) else None
        value = result.get("value") if isinstance(result, dict) else None
        data = value.get("data") if isinstance(value, dict) else None
        parsed = data.get("parsed") if isinstance(data, dict) else None
        info = parsed.get("info") if isinstance(parsed, dict) else None
        if not isinstance(value, dict) or value.get("owner") not in TOKEN_PROGRAMS:
            raise ValueError("The address is not an SPL Token or Token-2022 mint.")

        if (
            isinstance(parsed, dict)
            and parsed.get("type") == "mint"
            and isinstance(info, dict)
        ):
            try:
                decimals = int(info["decimals"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HeliusError("Solana returned invalid mint decimals.") from exc
        else:
            # RPC nodes may legitimately return base64 account data even when
            # jsonParsed was requested. getTokenSupply provides a canonical
            # mint check without requiring us to decode both token layouts.
            decimals = await self._get_token_supply_decimals(rpc_url, mint)
            if decimals is None:
                raise ValueError("The address is not an SPL Token or Token-2022 mint.")
        if not 0 <= decimals <= 255:
            raise HeliusError("Solana returned invalid mint decimals.")

        name: str | None = None
        symbol: str | None = None
        try:
            metadata_payload = await self._rpc_post_with_retry(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": "solducks-metadata",
                    "method": "getAsset",
                    "params": {"id": mint},
                },
            )
        except HeliusError:
            metadata_payload = {}
        if metadata_payload:
            metadata_result = (
                metadata_payload.get("result")
                if isinstance(metadata_payload, dict)
                else None
            )
            content = (
                metadata_result.get("content")
                if isinstance(metadata_result, dict)
                else None
            )
            metadata = content.get("metadata", {}) if isinstance(content, dict) else {}
            raw_name = metadata.get("name")
            raw_symbol = metadata.get("symbol")
            name = str(raw_name).strip()[:100] if raw_name else None
            symbol = str(raw_symbol).strip()[:20] if raw_symbol else None

        return WatchedToken(
            mint=mint,
            name=name,
            symbol=symbol,
            decimals=decimals,
            added_by=added_by,
        )

    async def _get_token_supply_decimals(self, rpc_url: str, mint: str) -> int | None:
        payload = await self._rpc_post_with_retry(
            rpc_url,
            {
                "jsonrpc": "2.0",
                "id": "solducks-mint-supply",
                "method": "getTokenSupply",
                "params": [mint],
            },
        )
        if not isinstance(payload, dict) or payload.get("error") is not None:
            return None
        result = payload.get("result")
        supply = result.get("value") if isinstance(result, dict) else None
        if not isinstance(supply, dict):
            return None
        try:
            return int(supply["decimals"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HeliusError("Solana returned invalid mint decimals.") from exc

    async def sync_webhook(self, database: Database) -> str | None:
        mints = [token.mint for token in await database.list_tokens()]
        return await self._sync_one_webhook(
            database,
            setting_key="helius_webhook_id",
            transaction_types={"SWAP", "BUY"},
            addresses=mints,
        )

    async def _sync_one_webhook(
        self,
        database: Database,
        *,
        setting_key: str,
        transaction_types: set[str],
        addresses: list[str],
    ) -> str | None:
        webhook_id = await database.get_setting(setting_key)
        remote: dict[str, Any] | None = None
        if webhook_id:
            candidate = await self._request("GET", webhook_id, allow_not_found=True)
            remote = candidate if isinstance(candidate, dict) else None
        if remote is None:
            candidates = await self._request("GET", None)
            if isinstance(candidates, list):
                remote = next(
                    (
                        item
                        for item in candidates
                        if isinstance(item, dict)
                        and item.get("webhookURL") == self.webhook_url
                        and set(item.get("transactionTypes") or {}) == transaction_types
                    ),
                    None,
                )
                if remote and isinstance(remote.get("webhookID"), str):
                    webhook_id = remote["webhookID"]
                    await database.set_setting(setting_key, webhook_id)

        if not addresses:
            if remote and remote.get("active") is True and webhook_id:
                await self._request("PATCH", webhook_id, {"active": False})
            return webhook_id or None
        body = {
            "webhookURL": self.webhook_url,
            "transactionTypes": sorted(transaction_types),
            "accountAddresses": addresses,
            "webhookType": "enhanced",
            "authHeader": self.webhook_secret,
            "txnStatus": "success",
        }
        if remote and webhook_id:
            remote_webhook_type = remote.get("webhookType")
            matches = (
                remote.get("webhookURL") == self.webhook_url
                and set(remote.get("transactionTypes") or {}) == transaction_types
                and set(remote.get("accountAddresses") or {}) == set(addresses)
                # Helius's documented GET response currently omits
                # webhookType. A present, conflicting value still forces an
                # update, while omission alone must not cause paid PUTs.
                and (remote_webhook_type is None or remote_webhook_type == "enhanced")
                and remote.get("authHeader") == self.webhook_secret
            )
            if matches and remote.get("active") is True:
                return webhook_id
            if matches:
                await self._request("PATCH", webhook_id, {"active": True})
                return webhook_id
            response = await self._request("PUT", webhook_id, body)
            if not isinstance(response, dict) or response.get("active") is not True:
                await self._request("PATCH", webhook_id, {"active": True})
            return webhook_id

        response = await self._request("POST", None, body)
        new_id = response.get("webhookID") if isinstance(response, dict) else None
        if not isinstance(new_id, str) or not new_id:
            raise HeliusError("Helius returned an invalid webhook identifier.")
        await database.set_setting(setting_key, new_id)
        return new_id

    async def _request(
        self,
        method: str,
        webhook_id: str | None,
        body: dict[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> Any:
        suffix = f"/{webhook_id}" if webhook_id else ""
        url = f"https://api-mainnet.helius-rpc.com/v0/webhooks{suffix}"
        try:
            response = await self.http.request(
                method,
                url,
                params={"api-key": self.api_key},
                json=body if body is not None else None,
            )
        except httpx.HTTPError as exc:
            raise HeliusError("Helius is temporarily unreachable.") from exc
        if allow_not_found and response.status_code == 404:
            return None
        if not response.is_success:
            detail = ""
            try:
                error_body = response.json()
                if isinstance(error_body, dict):
                    detail = f": {error_body.get('error') or error_body.get('message') or str(error_body)}"
            except ValueError:
                if response.text:
                    detail = f": {response.text[:200]}"
            logger.warning(
                "Helius webhook %s %s failed (HTTP %s)%s",
                method, url, response.status_code, detail,
            )
            raise HeliusError(
                f"Helius rejected the webhook update (HTTP {response.status_code})"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise HeliusError("Helius returned an invalid response.") from exc


class WatchlistService:
    """Serialize DB + remote webhook mutations and compensate on failure."""

    def __init__(self, database: Database, helius: HeliusClient) -> None:
        self.database = database
        self.helius = helius
        self._lock = asyncio.Lock()

    async def add(self, mint: str, user_id: int) -> tuple[WatchedToken, bool]:
        async with self._lock:
            existing = await self.database.get_token(mint)
            if existing:
                return existing, False
            token = await self.helius.validate_mint(mint, user_id)
            created = await self.database.add_token(token)
            if not created:
                concurrent = await self.database.get_token(mint)
                if concurrent:
                    return concurrent, False
                raise HeliusError(
                    "The watchlist changed concurrently; please try again."
                )
            try:
                await self.helius.sync_webhook(self.database)
            except Exception:
                try:
                    await self.database.remove_token(mint)
                finally:
                    await self._reconcile_after_rollback()
                raise
            return token, True

    async def remove(self, mint: str) -> WatchedToken | None:
        async with self._lock:
            token = await self.database.remove_token(mint)
            if token is None:
                return None
            try:
                await self.helius.sync_webhook(self.database)
            except Exception:
                try:
                    await self.database.add_token(token)
                finally:
                    await self._reconcile_after_rollback()
                raise
            return token

    async def _reconcile_after_rollback(self) -> None:
        """Best-effort repair after a webhook update partially succeeds."""
        try:
            await self.helius.sync_webhook(self.database)
        except Exception:
            logger.exception("Could not reconcile Helius after watchlist rollback")


def _decimal_amount(transfer: dict[str, Any]) -> Decimal:
    raw = transfer.get("rawTokenAmount")
    try:
        if isinstance(raw, dict) and raw.get("tokenAmount") is not None:
            decimals = int(raw.get("decimals", 0))
            return Decimal(str(raw["tokenAmount"])) / (Decimal(10) ** decimals)
        return Decimal(str(transfer.get("tokenAmount", 0)))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def parse_buys(
    payload: dict[str, Any], watched: dict[str, WatchedToken]
) -> list[BuyAlert]:
    """Extract net-positive monitored-token purchases from an enhanced event."""
    if payload.get("transactionError") is not None:
        return []
    event_type = payload.get("type")
    if event_type not in {"SWAP", "BUY"}:
        return []
    signature = payload.get("signature")
    if not isinstance(signature, str) or not signature:
        return []
    structured = _parse_structured_swap(payload, watched, signature)
    if structured is not None:
        return structured

    transfers = payload.get("tokenTransfers")
    if not isinstance(transfers, list):
        return []

    results: list[BuyAlert] = []
    for mint, token in watched.items():
        balances: dict[str, Decimal] = {}
        for item in transfers:
            if not isinstance(item, dict) or item.get("mint") != mint:
                continue
            amount = _decimal_amount(item)
            sender = item.get("fromUserAccount")
            recipient = item.get("toUserAccount")
            if isinstance(sender, str):
                balances[sender] = balances.get(sender, Decimal(0)) - amount
            if isinstance(recipient, str):
                balances[recipient] = balances.get(recipient, Decimal(0)) + amount
        # Enhanced transactions identify the initiator as feePayer. Restricting
        # the candidate to that account prevents a sell's receiving liquidity
        # pool from being misidentified as a buyer.
        fee_payer = payload.get("feePayer")
        if not isinstance(fee_payer, str) or balances.get(fee_payer, Decimal(0)) <= 0:
            continue
        buyer = fee_payer
        bought = balances[buyer]

        payment_amount: Decimal | None = None
        payment_symbol: str | None = None
        quote_totals: dict[str, Decimal] = {}
        for item in transfers:
            if not isinstance(item, dict) or item.get("fromUserAccount") != buyer:
                continue
            quote_symbol = QUOTE_MINTS.get(str(item.get("mint")))
            if quote_symbol:
                quote_totals[quote_symbol] = quote_totals.get(
                    quote_symbol, Decimal(0)
                ) + _decimal_amount(item)
        if quote_totals:
            payment_symbol, payment_amount = max(
                quote_totals.items(), key=lambda item: item[1]
            )
        else:
            native_total = Decimal(0)
            for item in payload.get("nativeTransfers") or []:
                if isinstance(item, dict) and item.get("fromUserAccount") == buyer:
                    try:
                        native_total += Decimal(str(item.get("amount", 0))) / Decimal(
                            1_000_000_000
                        )
                    except InvalidOperation:
                        pass
            if native_total > 0:
                payment_amount, payment_symbol = native_total, "SOL"

        # A parsed BUY/SWAP may route consideration through program-owned accounts;
        # payment is therefore optional, but a positive recipient balance is required.
        results.append(
            BuyAlert(
                token=token,
                buyer=buyer,
                token_amount=bought,
                payment_amount=payment_amount,
                payment_symbol=payment_symbol,
                signature=signature,
            )
        )
    return results


def _parse_structured_swap(
    payload: dict[str, Any],
    watched: dict[str, WatchedToken],
    signature: str,
) -> list[BuyAlert] | None:
    events = payload.get("events")
    swap = events.get("swap") if isinstance(events, dict) else None
    if not isinstance(swap, dict):
        return None
    outputs = swap.get("tokenOutputs")
    inputs = swap.get("tokenInputs")
    if not isinstance(outputs, list) or not isinstance(inputs, list):
        return None

    bought: dict[tuple[str, str], Decimal] = {}
    for output in outputs:
        if not isinstance(output, dict):
            continue
        mint = output.get("mint")
        buyer = output.get("userAccount")
        if mint in watched and isinstance(buyer, str):
            amount = _decimal_amount(output)
            if amount > 0:
                key = (mint, buyer)
                bought[key] = bought.get(key, Decimal(0)) + amount

    results: list[BuyAlert] = []
    for (mint, buyer), amount in bought.items():
        quote_totals: dict[str, Decimal] = {}
        for item in inputs:
            if not isinstance(item, dict) or item.get("userAccount") != buyer:
                continue
            symbol = QUOTE_MINTS.get(str(item.get("mint")))
            if symbol:
                quote_totals[symbol] = quote_totals.get(
                    symbol, Decimal(0)
                ) + _decimal_amount(item)

        payment_symbol: str | None = None
        payment_amount: Decimal | None = None
        if quote_totals:
            payment_symbol, payment_amount = max(
                quote_totals.items(), key=lambda item: item[1]
            )
        else:
            native_input = swap.get("nativeInput")
            if isinstance(native_input, dict) and native_input.get("account") == buyer:
                try:
                    lamports = Decimal(str(native_input.get("amount", 0)))
                except (InvalidOperation, TypeError, ValueError):
                    lamports = Decimal(0)
                if lamports > 0:
                    payment_amount = lamports / Decimal(1_000_000_000)
                    payment_symbol = "SOL"

        results.append(
            BuyAlert(
                token=watched[mint],
                buyer=buyer,
                token_amount=amount,
                payment_amount=payment_amount,
                payment_symbol=payment_symbol,
                signature=signature,
            )
        )
    return results
