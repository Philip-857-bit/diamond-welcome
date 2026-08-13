from decimal import Decimal
import unittest

from bot.alerts import build_caption
from bot.database import WatchedToken
from bot.solana import (
    USDC_MINT,
    USDT_MINT,
    WSOL_MINT,
    BuyAlert,
    is_public_key,
    parse_buys,
)


MINT = "So11111111111111111111111111111111111111112"
BUYER = "7YttLkHDoVGPnXeoBHb7XgCwgftUR9akTiGAZCnZBq2i"
POOL = "9xQeWvG816bUx9EPfEZKj4qNQwTPVDGMuQeN9pL9dS7p"


def watched() -> dict[str, WatchedToken]:
    token = WatchedToken(
        mint=MINT,
        name="Duck <Coin>",
        symbol="DUCK",
        decimals=6,
        added_by=1,
    )
    return {MINT: token}


class PublicKeyTests(unittest.TestCase):
    def test_accepts_32_byte_base58_key(self) -> None:
        self.assertTrue(is_public_key(MINT))
        self.assertTrue(is_public_key(BUYER))

    def test_rejects_invalid_alphabet_and_length(self) -> None:
        self.assertFalse(is_public_key("not-a-solana-key"))
        self.assertFalse(is_public_key("O" * 44))

    def test_quote_mints_are_valid_public_keys(self) -> None:
        self.assertTrue(is_public_key(USDC_MINT))
        self.assertTrue(is_public_key(USDT_MINT))


class BuyParserTests(unittest.TestCase):
    def test_uses_structured_swap_for_sponsored_buy_and_excludes_rent(self) -> None:
        sponsor = "Vote111111111111111111111111111111111111111"
        event = {
            "type": "SWAP",
            "signature": "signature-sponsored",
            "feePayer": sponsor,
            "transactionError": None,
            "events": {
                "swap": {
                    "nativeInput": {"account": BUYER, "amount": "750000000"},
                    "nativeOutput": None,
                    "tokenInputs": [],
                    "tokenOutputs": [
                        {
                            "userAccount": BUYER,
                            "tokenAccount": POOL,
                            "mint": MINT,
                            "rawTokenAmount": {
                                "tokenAmount": "12500000",
                                "decimals": 6,
                            },
                        }
                    ],
                }
            },
            "tokenTransfers": [],
            "nativeTransfers": [
                {
                    "fromUserAccount": BUYER,
                    "toUserAccount": POOL,
                    "amount": 750_000_000,
                },
                {
                    "fromUserAccount": BUYER,
                    "toUserAccount": sponsor,
                    "amount": 2_039_280,
                },
            ],
        }

        alert = parse_buys(event, watched())[0]
        self.assertEqual(alert.buyer, BUYER)
        self.assertEqual(alert.token_amount, Decimal("12.5"))
        self.assertEqual(alert.payment_amount, Decimal("0.75"))
        self.assertEqual(alert.payment_symbol, "SOL")

    def test_combines_structured_wsol_and_native_sol_inputs(self) -> None:
        token_mint = "HNCz9mGVK7hJwmR5PBJYCVnCiACNtbqTADuBeG6spump"
        token = WatchedToken(
            mint=token_mint,
            name="SoLDuck",
            symbol="SoLDuck",
            decimals=6,
            added_by=1,
        )
        event = {
            "type": "SWAP",
            "signature": "signature-mixed-sol-inputs",
            "feePayer": BUYER,
            "transactionError": None,
            "events": {
                "swap": {
                    "nativeInput": {"account": BUYER, "amount": "65110986"},
                    "nativeOutput": None,
                    "tokenInputs": [
                        {
                            "userAccount": BUYER,
                            "mint": WSOL_MINT,
                            "rawTokenAmount": {
                                "tokenAmount": "558187",
                                "decimals": 9,
                            },
                        }
                    ],
                    "tokenOutputs": [
                        {
                            "userAccount": BUYER,
                            "mint": token_mint,
                            "rawTokenAmount": {
                                "tokenAmount": "1262894322413",
                                "decimals": 6,
                            },
                        }
                    ],
                }
            },
            # These raw transfers deliberately include account rent. Structured
            # swap inputs, rather than the raw list, must determine the payment.
            "nativeTransfers": [
                {
                    "fromUserAccount": BUYER,
                    "toUserAccount": POOL,
                    "amount": 65_110_986,
                },
                {
                    "fromUserAccount": BUYER,
                    "toUserAccount": token_mint,
                    "amount": 2_074_080,
                },
            ],
            "tokenTransfers": [],
        }

        alert = parse_buys(event, {token_mint: token})[0]

        self.assertEqual(alert.token_amount, Decimal("1262894.322413"))
        self.assertEqual(alert.payment_amount, Decimal("0.065669173"))
        self.assertEqual(alert.payment_symbol, "SOL")

    def test_ignores_non_swap_activity_from_any_webhook(self) -> None:
        event = {
            "type": "TRANSFER",
            "signature": "signature-transfer",
            "feePayer": BUYER,
            "transactionError": None,
            "tokenTransfers": [
                {
                    "mint": MINT,
                    "fromUserAccount": POOL,
                    "toUserAccount": BUYER,
                    "tokenAmount": 10,
                }
            ],
        }
        self.assertEqual(parse_buys(event, watched()), [])

    def test_parses_net_positive_sol_buy(self) -> None:
        event = {
            "type": "SWAP",
            "signature": "signature-1",
            "feePayer": BUYER,
            "transactionError": None,
            "tokenTransfers": [
                {
                    "mint": MINT,
                    "fromUserAccount": POOL,
                    "toUserAccount": BUYER,
                    "rawTokenAmount": {"tokenAmount": "12500000", "decimals": 6},
                }
            ],
            "nativeTransfers": [
                {"fromUserAccount": BUYER, "toUserAccount": POOL, "amount": 750_000_000}
            ],
        }
        alerts = parse_buys(event, watched())
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].buyer, BUYER)
        self.assertEqual(alerts[0].token_amount, Decimal("12.5"))
        self.assertEqual(alerts[0].payment_amount, Decimal("0.75"))
        self.assertEqual(alerts[0].payment_symbol, "SOL")

    def test_parses_stablecoin_payment(self) -> None:
        event = {
            "type": "BUY",
            "signature": "signature-2",
            "feePayer": BUYER,
            "transactionError": None,
            "tokenTransfers": [
                {
                    "mint": MINT,
                    "fromUserAccount": POOL,
                    "toUserAccount": BUYER,
                    "tokenAmount": 42,
                },
                {
                    "mint": USDC_MINT,
                    "fromUserAccount": BUYER,
                    "toUserAccount": POOL,
                    "rawTokenAmount": {"tokenAmount": "1500000", "decimals": 6},
                },
            ],
            "nativeTransfers": [],
        }
        alert = parse_buys(event, watched())[0]
        self.assertEqual(alert.payment_amount, Decimal("1.5"))
        self.assertEqual(alert.payment_symbol, "USDC")

    def test_ignores_sell_and_failed_transaction(self) -> None:
        sell = {
            "type": "SWAP",
            "signature": "signature-3",
            "feePayer": BUYER,
            "transactionError": None,
            "tokenTransfers": [
                {
                    "mint": MINT,
                    "fromUserAccount": BUYER,
                    "toUserAccount": POOL,
                    "tokenAmount": 10,
                }
            ],
        }
        failed = {
            **sell,
            "signature": "signature-4",
            "transactionError": {"error": "x"},
        }
        self.assertEqual(parse_buys(sell, watched()), [])
        self.assertEqual(parse_buys(failed, watched()), [])

    def test_accounts_for_in_and_out_transfers(self) -> None:
        event = {
            "type": "SWAP",
            "signature": "signature-5",
            "feePayer": BUYER,
            "transactionError": None,
            "tokenTransfers": [
                {
                    "mint": MINT,
                    "fromUserAccount": POOL,
                    "toUserAccount": BUYER,
                    "tokenAmount": 10,
                },
                {
                    "mint": MINT,
                    "fromUserAccount": BUYER,
                    "toUserAccount": POOL,
                    "tokenAmount": 2,
                },
            ],
        }
        alert = parse_buys(event, watched())[0]
        self.assertEqual(alert.token_amount, Decimal("8"))

    def test_ignores_event_without_explicit_buy_type(self) -> None:
        event = {
            "signature": "signature-untyped",
            "feePayer": BUYER,
            "transactionError": None,
            "tokenTransfers": [
                {
                    "mint": MINT,
                    "fromUserAccount": POOL,
                    "toUserAccount": BUYER,
                    "tokenAmount": 10,
                }
            ],
        }
        self.assertEqual(parse_buys(event, watched()), [])


class CaptionTests(unittest.TestCase):
    def test_escapes_metadata_and_contains_transaction_link(self) -> None:
        token = watched()[MINT]
        caption = build_caption(
            BuyAlert(token, BUYER, Decimal("12.5"), None, None, "abc123")
        )
        self.assertIn("Duck &lt;Coin&gt;", caption)
        self.assertNotIn("Duck <Coin>", caption)
        self.assertIn("https://solscan.io/tx/abc123", caption)


if __name__ == "__main__":
    unittest.main()
