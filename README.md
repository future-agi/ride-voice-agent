# RideCo ride-booking voice agent

A local-first LiveKit voice worker for inbound ride booking. It follows the same
audio pipeline as the tested customer-support agent: Deepgram speech-to-text,
Gemini or Vertex for the language model, Deepgram text-to-speech, Silero voice
activity detection, and AI-coustics phone audio enhancement.

The application code, prompts, transactional state, tools API, fares, rider data,
OTP demo, and Postgres database all run on your machine. LiveKit Cloud remains the
media and SIP control plane, and Deepgram plus Gemini/Vertex remain model providers.
This is intentional: inbound LiveKit SIP telephony needs a reachable LiveKit
deployment. No application data service is hosted by this repository.

## What is enforced in code

- Pickup and destination must each be geocoded and explicitly confirmed.
- Fares, availability, surge, and pickup ETAs only come from the tools API.
- Changing an address invalidates the quote, payment choice, and consent token.
- Saved cards require a successful OTP in the current call.
- RideCo Cash must cover the high end of the quote; cash must be market-eligible.
- Payment links remain pending until a payment-provider callback marks them ready.
- Booking requires a one-time token tied to the exact trip read-back and an
  explicit affirmative response.
- Booking writes are idempotent, and cancellation fees are fetched before cancel.
- The database stores only card brand and last four digits, never PAN or CVV.

## Quick start

Requirements: Docker Desktop, Python 3.11–3.13, `uv`, a LiveKit Cloud project,
Deepgram credentials, and either Gemini API or Google Vertex credentials.

1. Create local configuration:

   ```bash
   cp .env.example .env.local
   ```

   Fill in `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`,
   `DEEPGRAM_API_KEY`, and `GEMINI_API_KEY`. For Vertex, use the three commented
   Google Cloud variables instead. Never commit `.env.local`.

2. Start Postgres and the tools API:

   ```bash
   docker compose up -d --build --wait
   curl http://localhost:18090/health
   ```

3. Install and validate the worker:

   ```bash
   uv sync --locked
   uv run python -m livekit.agents download-files
   uv run pytest -q
   ```

4. Talk to it locally in console mode:

   ```bash
   uv run python agent/agent.py console
   ```

   `DEMO_CALLER_ANI` chooses the seeded caller because console sessions have no
   SIP caller-ID attribute. The default is Dana (`+14155550101`). The demo OTP is
   `123456`.

5. Run the worker for rooms or telephony:

   ```bash
   uv run python agent/agent.py dev
   ```

   For a completely containerized application runtime:

   ```bash
   docker compose --profile full up --build
   ```

## Seeded scenarios

| Caller | Expected path |
|---|---|
| `+14155550101` Dana | Valid saved Visa; OTP required |
| `+14155550102` Marcus | Expired card; RideCo Cash can cover many trips |
| `+14155550103` Priya | Suspended; booking rejected and human handoff offered |
| `+919845550104` Arjun | Bengaluru cash-eligible market |
| Any other number | Guest flow; cash if eligible, otherwise payment link |

Useful spoken places include “Hilton Union Square,” “SFO International,” “two
hundred Market Street,” “Ferry Building,” and “Oakland airport.” “Main Street”
returns multiple cities on purpose so address disambiguation can be tested.

To simulate the external payment provider completing the latest guest link:

```bash
curl -X POST http://localhost:18090/demo/complete_payment_link \
  -H 'content-type: application/json' \
  -d '{"phone":"+14155550199"}'
```

Then ask the agent to check the payment-link status. In production, replace this
demo endpoint with an authenticated payment-provider webhook.

## Inbound phone setup

Install LiveKit CLI 2.15 or newer and authenticate it:

```bash
brew install livekit-cli
lk cloud auth
lk app env -w -d .env.local
```

Create or connect an inbound SIP trunk/LiveKit phone number, then apply the
included individual dispatch rule (one isolated room per caller):

```bash
lk sip dispatch create telephony/dispatch-rule.json
```

The dispatch rule deliberately leaves `hidePhoneNumber` false. The worker reads
the documented `sip.phoneNumber` participant attribute as an identity hint, while
OTP remains mandatory before charging saved cards.

## Verification and reset

Run deterministic unit tests only:

```bash
uv run pytest -q
```

Run the same tests plus the real container API/database flow:

```bash
RUN_INTEGRATION=1 uv run pytest -q
```

Format and lint:

```bash
uv run ruff format --check .
uv run ruff check .
```

Stop services while retaining demo data:

```bash
docker compose down
```

To deliberately erase and reseed the demo database, run
`docker compose down --volumes` and then `docker compose up -d --build`.

## Production replacements

The tools service is a complete local simulator, not RideCo's production backend.
Before real use, replace its generated drivers/fare engine, fixed OTP, SMS stubs,
payment callback, and logical human-transfer response with authenticated provider
integrations. Keep the guarded state machine and idempotency checks in front of
those integrations. Also add transcript redaction, encrypted secrets, rate limits,
jurisdiction-specific recording consent, and a real SIP warm/cold transfer.
