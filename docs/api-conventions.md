# API conventions aligned with LEMON

Verified against the public documentation on 2026-09-28. These are public API
conventions, not evidence of LEMON's internal Python style or architecture.
The lab remains an independent simulator, not a compatible brokerage replacement.

## Compatibility

Existing routes, `X-API-Key`, operator authentication, response shapes and the
visual panel remain unchanged. Opt into the new public contract using `/v1`.
Both versions share ownership checks, accounting and durable idempotency records.
A retry crossing API versions addresses the same business intention, but its
response is rendered in that version's format. Stored evidence is not rewritten.

## Implemented conventions

| Area | `/v1` behavior | Source |
|---|---|---|
| Authentication | `Authorization: Bearer <API key>` | [Authorization](https://developer.lemon.markets/docs/authorization) |
| Audit context | Require `LMG-Data-Privacy-Access-Principal` and `LMG-Data-Privacy-Access-Justification`; log both with the authenticated client. These claims never grant access. | [Authorization](https://developer.lemon.markets/docs/authorization) |
| Numbers | Cash and transaction amounts: decimal strings, 2 places. Unit prices: 4 places. Share quantities: strings, 5 places. Decimal/integer arithmetic only. | [Number formats](https://developer.lemon.markets/docs/fundamental-number-formats) |
| Timestamps | ISO 8601 UTC with millisecond precision and `Z` on output. Internal evidence retains full precision. Shared input type accepts zoned ISO timestamps and rejects naive dates/numeric epochs. Date-only fields, if added, use `YYYY-MM-DD`. | [Date/time](https://developer.lemon.markets/docs/fundamental-date-and-time-formats) |
| Errors | `message`; validation errors also provide locations and messages, excluding submitted values. HTTP status codes retain their meaning. | [Idempotency examples](https://developer.lemon.markets/docs/idempotency) |
| Lists | `data` plus `pagination.next_cursor`. Positions accept `limit` 1–100 and an account-scoped opaque cursor. Timeline returns the complete order history as one page. | [Positions](https://developer.lemon.markets/reference/get_positions-1) |
| JSON | Existing snake_case field names and ISO currency code `EUR`; order side is `buy` in the new interface. | [Create order](https://developer.lemon.markets/reference/create_order-1) |
| Idempotency | New interface limits keys to 100 characters; same intention replays, changed payload returns 422. | [Idempotency](https://developer.lemon.markets/docs/idempotency) |

## Deliberate differences

- Keys remain **required and permanent**, as in the existing lab. LEMON
  documents optional keys and 24-hour expiry. Adopting that here could create a
  second purchase when an old request is replayed. No records are expired/deleted.
- Whole shares only. Decimal strings such as `"8.00000"` are accepted; actual
  fractional purchases are rejected, never rounded. Integer input remains an alias.
- `SYNTH-100` is a synthetic instrument (displayed as fictional LEMON). It is not
  an ISIN. Existing local IDs/status transitions are not renamed into broker IDs.
- Partner-simulator HTTP contracts and operator endpoints retain their original
  formats. They are internal lab interfaces, not the public `/v1` contract.
- SCA/WebAuthn, webhooks and a real LEMON client are separate features.
  Bearer authentication here still uses configured local demo credentials.
- Public documentation examples sometimes contain microseconds, while the
  date/time guide specifies milliseconds. The new API follows the guide for
  display without reducing precision in execution deduplication or accounting.

## Example

```bash
curl http://127.0.0.1:8000/v1/demo/orders \
  -H "Authorization: Bearer $DEMO_API_KEY" \
  -H 'LMG-Data-Privacy-Access-Principal: backend-demo-client' \
  -H 'LMG-Data-Privacy-Access-Justification: app_usage-order-entry' \
  -H 'Idempotency-Key: a-new-unique-order-intention' \
  -H 'Content-Type: application/json' \
  --data '{"quantity":"8.00000","side":"buy"}'
```

The new contract exposes demo/account cash, order creation/reading, order timeline,
account positions and ledger balance. Swagger documents both authentication
schemes. The existing Postman requests remain available; the added v1 folder
checks the new contract against an account created by the preceding scenarios.
