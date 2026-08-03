"""Helius webhook management, mint validation, and buy-event parsing."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
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
MAX_PENDING_EVENT_BATCHES = 20


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
        max_monitored_addresses: int = 100_000,
    ) -> None:
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.webhook_url = f"{public_base_url.rstrip('/')}/{webhook_path.strip('/')}"
        self.max_monitored_addresses = max_monitored_addresses
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))

    async def close(self) -> None:
        await self.http.aclose()

    async def validate_mint(self, mint: str, added_by: int) -> WatchedToken:
        if not is_public_key(mint):
            raise ValueError("That is not a valid Solana mint address.")

        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"
        try:
            account_response = await self.http.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": "solducks-mint",
                    "method": "getAccountInfo",
                    "params": [mint, {"encoding": "jsonParsed"}],
                },
            )
        except httpx.HTTPError as exc:
            raise HeliusError("Could not reach Solana to validate that mint.") from exc
        self._raise(account_response, "Could not validate the mint on Solana.")
        try:
            account_data = account_response.json()
        except ValueError as exc:
            raise HeliusError("Solana returned an invalid mint response.") from exc
        result = account_data.get("result") if isinstance(account_data, dict) else None
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
            metadata_response = await self.http.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": "solducks-metadata",
                    "method": "getAsset",
                    "params": {"id": mint},
                },
            )
            metadata_data = (
                metadata_response.json() if metadata_response.is_success else {}
            )
        except (httpx.HTTPError, ValueError):
            metadata_data = {}
        if metadata_data:
            metadata_result = (
                metadata_data.get("result") if isinstance(metadata_data, dict) else None
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
        try:
            response = await self.http.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": "solducks-mint-supply",
                    "method": "getTokenSupply",
                    "params": [mint],
                },
            )
        except httpx.HTTPError as exc:
            raise HeliusError("Could not reach Solana to validate that mint.") from exc
        self._raise(response, "Could not validate the mint on Solana.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise HeliusError("Solana returned an invalid mint response.") from exc
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

    async def discover_token_accounts(self, mint: str) -> set[str]:
        """Find token accounts whose first 32 data bytes reference this mint."""
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"
        discovered: set[str] = set()
        for program_id in TOKEN_PROGRAMS:
            try:
                response = await self.http.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": "solducks-token-accounts",
                        "method": "getProgramAccounts",
                        "params": [
                            program_id,
                            {
                                "encoding": "base64",
                                "dataSlice": {"offset": 0, "length": 0},
                                "filters": [{"memcmp": {"offset": 0, "bytes": mint}}],
                            },
                        ],
                    },
                )
            except httpx.HTTPError as exc:
                raise HeliusError(
                    "Could not discover token accounts for that mint."
                ) from exc
            self._raise(response, "Could not discover token accounts for that mint.")
            try:
                payload = response.json()
            except ValueError as exc:
                raise HeliusError(
                    "Solana returned invalid token-account data."
                ) from exc
            if not isinstance(payload, dict) or payload.get("error"):
                raise HeliusError(
                    "Solana rejected the token-account discovery request."
                )
            result = payload.get("result")
            if not isinstance(result, list):
                raise HeliusError("Solana returned invalid token-account data.")
            for item in result:
                pubkey = item.get("pubkey") if isinstance(item, dict) else None
                if isinstance(pubkey, str) and is_public_key(pubkey):
                    discovered.add(pubkey)
        return discovered

    async def sync_webhook(self, database: Database) -> str | None:
        addresses = await database.list_monitored_addresses()
        mints = [token.mint for token in await database.list_tokens()]
        if len(addresses) > self.max_monitored_addresses:
            raise HeliusError(
                f"The watchlist requires {len(addresses):,} monitored addresses; "
                f"Helius allows {self.max_monitored_addresses:,}."
            )

        # The coverage webhook sees mint-referencing account creation activity,
        # while the alert webhook remains limited to actionable transactions.
        await self._sync_one_webhook(
            database,
            setting_key="helius_coverage_webhook_id",
            transaction_types={"ANY"},
            addresses=mints,
        )
        return await self._sync_one_webhook(
            database,
            setting_key="helius_webhook_id",
            transaction_types={"SWAP", "BUY"},
            addresses=addresses,
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
        self._raise(response, "Helius rejected the webhook update.")
        try:
            return response.json()
        except ValueError as exc:
            raise HeliusError("Helius returned an invalid response.") from exc

    @staticmethod
    def _raise(response: httpx.Response, message: str) -> None:
        if not response.is_success:
            raise HeliusError(f"{message} (HTTP {response.status_code})")


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
            token_accounts = await self.helius.discover_token_accounts(mint)
            current_addresses = set(await self.database.list_monitored_addresses())
            required = current_addresses | token_accounts | {mint}
            if len(required) > self.helius.max_monitored_addresses:
                raise ValueError(
                    f"Adding this token would require {len(required):,} monitored addresses; "
                    f"the Helius limit is {self.helius.max_monitored_addresses:,}."
                )
            created = await self.database.add_token(token)
            if not created:
                concurrent = await self.database.get_token(mint)
                if concurrent:
                    return concurrent, False
                raise HeliusError(
                    "The watchlist changed concurrently; please try again."
                )
            try:
                await self.database.replace_token_accounts(mint, token_accounts)
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
            token_accounts = await self.database.get_token_accounts(mint)
            token = await self.database.remove_token(mint)
            if token is None:
                return None
            try:
                await self.helius.sync_webhook(self.database)
            except Exception:
                try:
                    await self.database.add_token(token)
                    await self.database.replace_token_accounts(mint, token_accounts)
                finally:
                    await self._reconcile_after_rollback()
                raise
            return token

    async def refresh_all(self) -> bool:
        """Refresh token-account coverage and reconcile Helius once per batch."""
        async with self._lock:
            tokens = await self.database.list_tokens()
            discoveries: dict[str, set[str]] = {}
            all_addresses = {token.mint for token in tokens}
            for token in tokens:
                accounts = await self.helius.discover_token_accounts(token.mint)
                discoveries[token.mint] = accounts
                all_addresses.update(accounts)
            if len(all_addresses) > self.helius.max_monitored_addresses:
                raise HeliusError(
                    f"Token-account refresh found {len(all_addresses):,} addresses; "
                    f"the Helius limit is {self.helius.max_monitored_addresses:,}."
                )
            changed = False
            for mint, accounts in discoveries.items():
                changed = (
                    await self.database.replace_token_accounts(mint, accounts)
                    or changed
                )
            await self.helius.sync_webhook(self.database)
            return changed

    async def add_observed_accounts(self, observed: dict[str, set[str]]) -> bool:
        """Merge token accounts learned directly from enhanced webhook events."""
        async with self._lock:
            watched = {token.mint for token in await self.database.list_tokens()}
            relevant = {
                mint: addresses
                for mint, addresses in observed.items()
                if mint in watched and addresses
            }
            if not relevant:
                return False

            previous: dict[str, set[str]] = {}
            additions: set[str] = set()
            for mint, addresses in relevant.items():
                current = await self.database.get_token_accounts(mint)
                previous[mint] = current
                additions.update(addresses - current)
            if not additions:
                return False
            current_addresses = set(await self.database.list_monitored_addresses())
            if len(current_addresses | additions) > self.helius.max_monitored_addresses:
                raise HeliusError(
                    "Observed token accounts exceed the configured Helius address limit."
                )

            changed_mints: list[str] = []
            try:
                for mint, addresses in relevant.items():
                    if await self.database.replace_token_accounts(
                        mint, previous[mint] | addresses
                    ):
                        changed_mints.append(mint)
                await self.helius.sync_webhook(self.database)
            except Exception:
                try:
                    for mint in changed_mints:
                        await self.database.replace_token_accounts(mint, previous[mint])
                finally:
                    await self._reconcile_after_rollback()
                raise
            return bool(changed_mints)

    async def _reconcile_after_rollback(self) -> None:
        """Best-effort repair after a multi-webhook update partially succeeds."""
        try:
            await self.helius.sync_webhook(self.database)
        except Exception:
            # PostgreSQL remains the source of truth. The periodic refresher
            # will retry reconciliation if Helius is still unavailable.
            logger.exception("Could not reconcile Helius after watchlist rollback")


def extract_token_accounts(events: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Extract mint-to-token-account relationships from enhanced events."""
    observed: dict[str, set[str]] = {}
    for event in events:
        transfers = event.get("tokenTransfers")
        if isinstance(transfers, list):
            for transfer in transfers:
                if not isinstance(transfer, dict):
                    continue
                mint = transfer.get("mint")
                if not isinstance(mint, str) or not is_public_key(mint):
                    continue
                for key in ("fromTokenAccount", "toTokenAccount"):
                    address = transfer.get(key)
                    if isinstance(address, str) and is_public_key(address):
                        observed.setdefault(mint, set()).add(address)

        account_data = event.get("accountData")
        if not isinstance(account_data, list):
            continue
        for account in account_data:
            changes = (
                account.get("tokenBalanceChanges")
                if isinstance(account, dict)
                else None
            )
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict):
                    continue
                mint = change.get("mint")
                address = change.get("tokenAccount")
                if (
                    isinstance(mint, str)
                    and isinstance(address, str)
                    and is_public_key(mint)
                    and is_public_key(address)
                ):
                    observed.setdefault(mint, set()).add(address)
    return observed


class TokenAccountRefresher:
    def __init__(self, watchlist: WatchlistService, interval_seconds: float) -> None:
        self.watchlist = watchlist
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._event_batches: deque[list[dict[str, Any]]] = deque()
        self._observed: dict[str, set[str]] = {}
        self._full_refresh_requested = False

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def observe(self, events: list[dict[str, Any]]) -> None:
        """Queue raw events in O(1); extraction happens in the background loop."""
        if len(self._event_batches) < MAX_PENDING_EVENT_BATCHES:
            self._event_batches.append(events)
        else:
            # Bound memory during bursts. Full discovery safely recovers any
            # token-account observations omitted from the in-memory queue.
            self._full_refresh_requested = True
        self._wake.set()

    async def run(self) -> None:
        first_run = True
        while not self._stop.is_set():
            failed = False
            try:
                batches, self._event_batches = self._event_batches, deque()
                observed, self._observed = self._observed, {}
                full_refresh = first_run or self._full_refresh_requested
                first_run = False
                self._full_refresh_requested = False
                self._wake.clear()
                for events in batches:
                    extracted = extract_token_accounts(events)
                    for mint, addresses in extracted.items():
                        observed.setdefault(mint, set()).update(addresses)
                    # Account initialization can contain no balance change. An
                    # event with no extractable account requests full discovery.
                    if not extracted:
                        full_refresh = True
                if observed:
                    await self.watchlist.add_observed_accounts(observed)
                if full_refresh:
                    await self.watchlist.refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
                for mint, addresses in observed.items():
                    self._observed.setdefault(mint, set()).update(addresses)
                self._full_refresh_requested = (
                    self._full_refresh_requested or full_refresh
                )
                logger.exception("Token-account coverage refresh failed")
            delay = (
                min(60.0, self.interval_seconds) if failed else self.interval_seconds
            )
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                self._full_refresh_requested = True


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
