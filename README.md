<h1 align="center">Telegram News Aggregator</h1>

<p align="center">
  A production-grade Telegram bot that collects posts from public channels,
  deduplicates them, stores them in PostgreSQL and republishes them to your own
  channels — with subscriptions, trials, payments and an admin panel.
</p>

<p align="center">
  <a href="https://t.me/dekelia_bot"><b>▶ Try the live bot — @dekelia_bot</b></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python 3.11">
  <img src="https://img.shields.io/badge/aiogram-3.31-2CA5E0?logo=telegram&logoColor=white" alt="aiogram 3.31">
  <img src="https://img.shields.io/badge/SQLAlchemy-2.0-D71F00" alt="SQLAlchemy 2.0">
  <img src="https://img.shields.io/badge/PostgreSQL-15-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 15">
  <img src="https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white" alt="Redis 7">
  <img src="https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <img src="https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white" alt="pytest">
</p>

<p align="center">
  <b>English</b> · <a href="README.ru.md">Русский</a>
</p>

---

## What it does

The bot reads public Telegram channels through their web preview
(`t.me/s/<channel>`), stores every post in PostgreSQL and shows it to the user
with text and media attached. On top of that sits a full product: paid plans,
a trial period, referrals, link tracking and scheduled republishing.

| Feature | Description |
|---|---|
| **News feed** | Reads any public channel through its web preview — no userbot, no MTProto, no phone number |
| **Deduplication** | The same story from five channels collapses into one entry: SHA-256 → simhash bands → Jaccard/Levenshtein |
| **Full-text search** | `/search` over the archive with PostgreSQL `tsvector`, stemming, ranking and highlighting |
| **Auto-posting** | Republishes selected news to your own channels through a queue with retries |
| **Word filter** | Per-user trigger words and stop words |
| **Subscriptions** | Paid plans with a trial period and multi-account protection |
| **Payments** | Telegram Stars and crypto (CryptoBot), both idempotent end to end |
| **Link tracking** | Rewrites outbound links, counts clicks and unique visitors, reports through `/stats` |
| **Growth** | Referral programme and promo codes |
| **Admin panel** | Business metrics and a rate-limited broadcast engine |
| **Localisation** | Russian, English and Ukrainian, plural forms included |
| **Anti-flood** | Token bucket in Redis, sliding window, single-flight and locks on critical actions |

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.11** | `asyncio` throughout: the bot, the worker and the webhook server are all I/O-bound |
| Bot framework | **aiogram 3** | Native async Bot API client with routers, FSM, middlewares and typed `callback_data` |
| Database | **PostgreSQL 15** | The correctness of this product lives in the schema: unique and partial indexes, CHECKs, `FOR UPDATE SKIP LOCKED`, advisory locks, generated `tsvector` columns |
| ORM | **SQLAlchemy 2.0** (async) | Typed declarative models over `asyncpg`, with a repository layer as the only place that speaks SQL |
| Migrations | **Alembic** | The application never mutates the schema — only migrations do |
| Cache / locks | **Redis 7** | FSM storage, rate-limit buckets, the dedup window, the click buffer and distributed locks |
| Scheduler | **APScheduler** | Periodic jobs in a separate worker process |
| HTTP | **aiohttp** | Channel fetching, media downloads, the CryptoBot API and the webhook/redirect server |
| Parsing | **BeautifulSoup 4** | HTML of the channel web preview |
| Tests | **pytest** + **pytest-asyncio** | Real PostgreSQL and `fakeredis[lua]`, time frozen with `freezegun` |
| Packaging | **Docker Compose** | Bot, worker, PostgreSQL and Redis as four services |

### Key libraries

```text
aiogram==3.31.0          Telegram Bot API framework (routers, FSM, middlewares)
SQLAlchemy==2.0.29       async ORM, typed declarative models
asyncpg==0.29.0          PostgreSQL driver
alembic==1.13.1          schema migrations
redis==5.0.3             async Redis client (rate limits, FSM, locks, buffers)
APScheduler==3.10.4      background job scheduling
aiohttp==3.9.3           async HTTP client and webhook server
beautifulsoup4==4.12.3   HTML parsing of channel previews
python-dotenv==1.0.1     configuration from .env

pytest==9.1.1            test runner
pytest-asyncio==1.4.0    async tests
pytest-cov==7.0.0        coverage
freezegun==1.5.1         frozen time (with real_asyncio=True)
fakeredis[lua]==2.26.2   Redis backend under test, Lua scripts included
```

## Quick start

```bash
git clone https://github.com/vindibee/Telegram-News-Aggregator.git
cd Telegram-News-Aggregator
cp .env.example .env          # set BOT_TOKEN and the database password
docker compose -f Docker/docker-compose.yml up -d --build
```

Everything else — local setup without Docker, the full environment reference
and the design notes behind every subsystem — is documented below.

## Architecture

The layers are split by responsibility and dependencies point strictly
downwards:

```
main.py                 composition root: builds the bot, the DB and the HTTP session
└── tg_bot/             Telegram layer
    ├── handlers/       input parsing and service calls (no SQL, no HTTP)
    │   └── cabinet.py  personal cabinet: sources, targets, word filter
    ├── views.py        post card rendering
    ├── keyboards.py    inline keyboards
    ├── callbacks.py    typed callback_data
    ├── middlewares/    dependencies, throttling, double-tap protection
    ├── flags.py        handler flags (per-handler limits)
    ├── states.py       FSM states
    ├── errors.py       global exception handling
    └── utils.py        safe editing, text chunking, retries
└── services/           application layer
    ├── parser.py       channel HTML parsing
    ├── media.py        attachment download with a size limit
    ├── news_service.py the "show" and "refresh" scenarios
    ├── billing/        subscription payment by Stars and crypto
    ├── trial/          trial period and multi-account protection
    ├── notifier.py     delivery that respects Bot API limits
    ├── search.py       full-text search over the archive
    ├── tracker.py      link rewriting, click accounting, reports
    ├── dedup.py        collapsing repeated news
    ├── dedup_index.py  recent-news window in Redis
    ├── fingerprint.py  text fingerprints and similarity measures
    └── ratelimit/      rate limiting and anti-flood
└── db/                 data access
    ├── models/         ORM models (SQLAlchemy 2.0)
    │   ├── user.py     users, language, trial fingerprints
    │   ├── channel.py  sources and publication targets
    │   ├── referral.py referral rewards
    │   ├── keyword.py  filter triggers and stop words
    │   ├── schedule.py queue of scheduled publications
    │   ├── promo.py    promo codes and their redemptions
    │   └── tracking.py short links and the click log
    ├── enums.py        domain enums and native PostgreSQL ENUMs
    ├── mixins.py       shared model fragments (PK, timestamps)
    ├── exceptions.py   domain exceptions
    ├── repositories/   repositories: the only place with SQL
    ├── uow.py          Unit of Work — the transaction boundary
    ├── locks.py        PostgreSQL advisory locks
    └── database.py     engine and session factory
└── migrations/         Alembic migrations
└── worker/             background worker (separate process)
    ├── __main__.py     entry point: python -m worker
    ├── runner.py       job scheduler on top of APScheduler
    └── tasks/          periodic jobs (subscriptions, invoices, publications)
└── locales/            translation catalogues (ru, en, uk)
└── web/                payment webhooks and short-link redirects
└── core/               configuration and logging
```

## Data model

| Table | Purpose | Key guarantees at the database level |
|---|---|---|
| `users` | Users, interface language, referrals, trial usage | `UNIQUE(telegram_id)`, `UNIQUE(referral_code)`, self-referral forbidden |
| `trial_claims` | Fingerprints protecting the trial | `UNIQUE(kind, fingerprint)` — a second trial from the same phone/IP is impossible |
| `subscriptions` | Subscriptions and their periods | Partial `UNIQUE(user_id) WHERE status IN ('trialing','active')` — one live subscription |
| `subscription_events` | Operation log | `UNIQUE(payment_id)` — one payment cannot grant days twice |
| `payments` | Payments | `UNIQUE(provider, invoice_id)`, `UNIQUE(provider, external_id)`, `UNIQUE(idempotency_key)`, `amount > 0` |
| `posts` | News | `UNIQUE(channel_name, message_id)`, generated `tsvector` + GIN, `content_hash` and simhash bands for deduplication |
| `user_channels` | Sources and publication targets | `UNIQUE(user_id, kind, username)` and `UNIQUE(user_id, kind, chat_id)`; a target must have a `chat_id` |
| `user_keywords` | Filter triggers and stop words | `UNIQUE(user_id, kind, word)`, CHECK `word = lower(word)` |
| `scheduled_posts` | Queue of scheduled publications | `UNIQUE(target_channel_id, post_id)`; a published row must carry a `message_id` |
| `referrals` | Referral rewards | `UNIQUE(referred_id)` — an invitee counts once; self-referral forbidden |
| `promocodes` | Promo codes | `UNIQUE(code)`, `activations <= max_activations`, discount capped at 100% |
| `promocode_redemptions` | Promo code redemptions | `UNIQUE(promocode_id, user_id)` — one code per user |
| `tracked_links` | Short links | `UNIQUE(token)`, `unique_clicks <= clicks` |
| `click_logs` | Click log | `UNIQUE(link_id, visitor_hash)` — a unique visit is counted once |

No personal data is stored anywhere: the phone number in `trial_claims` and the
"IP + User-Agent" pair in `click_logs` become HMAC-SHA256 with a server-side
secret. A plain hash would not be enough — both the space of phone numbers and
the whole IPv4 space can be brute-forced in minutes.

**Why sources and publication targets share one table.** Their field sets are
identical; the only difference is a single `kind` value. Two nearly identical
tables would mean duplicated indexes, constraints and repository code. The role
is part of the uniqueness key, so the same channel can be read from and
published to at once.

**Why referrals live in their own table when `users.referred_by_id` exists.**
The column in `users` records the fact of the relation; `referrals` records the
lifecycle of the reward: when the invitation qualified, against which payment,
and how many days have already been granted. Without that row, a redelivered
payment webhook would grant the bonus a second time. The reward is tied to the
invitee's first payment rather than to registration — otherwise the programme
would be profitable to farm with empty accounts.

**Why the publication queue stores `message_id`.** Without it you can neither
edit a publication, nor delete it, nor tell "sent" from "seems sent". A CHECK
prevents moving a row into `published` without recording the message id.

**Why one failure does not drop a post from the queue.** The attempt counter
grows on every failure, but the status only changes once attempts run out:
a publication failure is almost always temporary — flood control or a channel
being briefly unreachable.

**Why counters are denormalised.** `promocodes.activations`,
`tracked_links.clicks` and `unique_clicks` are stored in the row instead of
being derived with `COUNT(*)`. A promo code's limit is checked on every
redemption, and the click log is the fastest-growing table in the product.
Divergence is ruled out because the counter changes in the same transaction as
the fact it counts, and CHECK constraints keep it inside its bounds.

**Interface language.** `users.language` is the user's deliberate choice, while
`users.language_code` is a hint from the Telegram client. They must not be
mixed: someone with an English system may well want a Russian interface, and a
device setting must not silently override that decision.

Full-text search uses the `russian` configuration with stemming, and
`search_vector` is declared as a generated column — index and text cannot drift
apart by construction.

## Onboarding

The first screen used to be the channel list — which only makes sense to
someone who already knows why they came. The entry point is now three short
steps.

1. **`/start`** — a bilingual greeting and a single "Start" button. Single on
   purpose: someone opening the bot for the first time knows nothing about it
   yet, and a choice of ten sections at that moment gets in the way rather than
   helping.
2. **Language choice** — before any meaningful text. The reverse order would
   mean the first and most important screen is read in whatever language
   Telegram supplied, not the one the person picked.
3. **What the bot does** — already in the chosen language: what it is for and
   what it can do.

Next comes a main menu of **features**, not of channels: a channel is one of
the capabilities, not the product itself. The channel list did not go anywhere
and opens from the menu as "News feed".

Help is organised as a catalogue: the "How to use" section lists nine features,
each with its own screen in three parts — *what it is*, *how to use it*, *why
it matters*. The flat list of commands is still there but moved to a separate
screen: commands help those who already found their footing and tell a newcomer
nothing.

All of it is fully localised — buttons and explanations alike. Switching the
language at any moment redraws both.

## Archive search

`/search` is full-text search with ranking and highlighting. It is a paid-plan
feature, so access is checked both when the query is entered and when pages are
turned: a subscription can expire between two pages.

**One configuration for two languages.** `to_tsvector('russian', …)` stems both
Cyrillic and Latin: `releases`, `released` and `releasing` all yield the lemma
`releas`. A separate column for English is unnecessary.

**`websearch_to_tsquery`, not `to_tsquery`.** The former accepts what people
type anyway — quotes for an exact phrase, a minus for exclusion — and does not
fail on arbitrary input, whereas the latter needs operator syntax where any
typo becomes an error.

**Highlighting is marked with control characters, not tags.** News text
contains arbitrary characters, `<` and `&` included. If PostgreSQL inserted
`<b>` directly, escaping the result afterwards would be impossible — the markup
would be indistinguishable from the text's own angle brackets, and Telegram
would reject the message. So `ts_headline` marks matches with characters that
cannot occur in the text, and the presentation layer turns them into tags after
escaping.

**The total number of results is deliberately absent.** `COUNT(*)` over a
full-text query costs roughly as much as the query itself. Instead, one row
more than the page is requested: if it arrives, there is a next page.

**The query lives in FSM state, not in `callback_data`.** That field holds 64
bytes, and size is not the only issue: the data comes from the client, and a
tampered string would surface someone else's results under the guise of paging.

Duplicates are excluded from results — otherwise one story would fill half a
page.

## News deduplication

The same story arrives from several channels: somewhere verbatim, somewhere
with a "subscribe" call-to-action appended. The decision is made in three
stages, from cheap to expensive.

| Stage | Mechanism | What it catches |
|---|---|---|
| Exact match | SHA-256 of the normalised text, looked up by index | Verbatim reprints — the majority |
| Candidate selection | A matching simhash band **or** full-text search over shared vocabulary | Dozens of rows instead of the whole table |
| Confirmation | Hamming distance, then Jaccard over shingles or Levenshtein | Filters out false pairs |

**Why two selection channels.** Simhash bands (LSH by banding) only find nearly
identical texts: with four 16-bit bands, the differing bits of a reprint with an
extra paragraph land in every band at once, and the candidate is not found at
all. The full-text index, built for search anyway, matches on shared vocabulary
and does find such pairs.

**Why these thresholds.** Measured on news pairs: genuine reprints give a
Hamming distance of 0–8 and Jaccard of 0.85–1.00, while unrelated news gives
23–31 and 0.00–0.08. The Hamming threshold sits in the middle of that gap (16)
and acts as a high-recall filter, while the decision is made by Jaccard at 0.75.
Swapping the roles — strict Hamming, lenient Jaccard — would lose genuine
duplicates.

**Short messages** are compared with Levenshtein: a five-word text has only
three three-word shingles, and a single changed word drops Jaccard to almost
zero.

**What the algorithm does not catch:** a rewrite retold in someone's own words,
and transliteration ("Twitter" vs "Твиттер"). Lexical measures see different
texts there — such cases need vector representations.

**The last 48 hours are mirrored in Redis.** Candidates could be looked up in
PostgreSQL alone, but that means two queries per incoming row, the second of
them heavy. The window is narrow and holds thousands of rows, which fit in
memory comfortably.

Redis accelerates here; PostgreSQL guarantees. The index is a cache, not a
source of truth: it can be empty after a restart or unavailable entirely, so a
miss never means "no duplicates". On a miss the exact match is verified in the
database, band selection is always complemented by the database, and what is
saved is the most expensive step — full-text search — and only when there are
already enough candidates.

For one-off checks there are `is_duplicate(text, threshold)` and
`find_duplicate(text)`, which returns the original's id and the similarity as a
percentage. For a batch from the parser, `classify` is still used: it also
compares the incoming rows against each other.

Duplicates are not deleted: they are stored with status `duplicate` and a
`duplicate_of_id` reference to the original, but hidden from results. That
keeps the option of rebuilding clusters when thresholds change, and of counting
how many repeats are being filtered out.

## Background worker

A separate process and a separate container: the worker has its own load
profile, can be scaled independently, and a failing background job must not
take message handling down with it. The worker does not migrate the schema —
the bot does.

```bash
python -m worker
```

| Job | Interval | What it does |
|---|---|---|
| `subscription_expiry_notice` | 15 min | Warns a day before the subscription ends |
| `subscription_expiration` | 5 min | Revokes access for expired subscriptions |
| `stale_invoice_cleanup` | 10 min | Expires unpaid invoices |
| `scheduled_post_publisher` | 30 s | Publishes scheduled news into target channels |
| `click_flush` | 60 s | Moves link clicks from Redis into the database |

All jobs consume their queue with `FOR UPDATE SKIP LOCKED`, so several worker
replicas never process one subscription twice.

**Compensation on delivery failure.** The notification mark is set together
with the claim, in a single query — otherwise two workers would notify the user
twice. But since the mark is set *before* sending, it has to be cleared when
delivery fails: the next run would not see that subscription again and the
notice would be lost forever. The exception is users who blocked the bot:
retrying is pointless, they are flagged in `users.is_bot_blocked` and excluded
from future mailings.

Access revocation deliberately has no compensation: if the message did not
arrive, the subscription must still end.

**Publishing happens outside the claiming transaction.** Holding a transaction
open while the bot talks to Telegram means holding row locks for the duration
of a network exchange: seconds under flood control, minutes when a channel is
unavailable. The queue is claimed, the transaction closes, the sends happen,
and the result is written in a separate transaction.

**The status changes after the fact, not before it.** Marking "published" up
front would be more convenient for idempotency, but a worker that dies between
the mark and the send would lose the publication forever. A repeated send at
worst produces a duplicate in the channel; a loss produces silence where the
user expected a post.

**The subscription is checked at publication time.** Hours pass between
queueing and sending, and a subscription can expire in between: publishing
anyway means giving away a paid feature for free.

**`copy_message` with a text fallback.** Copying carries the original
formatting and media across as they are, but only works while the bot can still
see the source message — the channel may have been closed, the post deleted.
Then the stored text is used: publishing the news without formatting beats not
publishing it.

**Losing rights is irreversible by itself**, so instead of three attempts the
channel is flagged as lacking rights and its entire queue is cancelled in a
single query. Remaining publications to the same channel in the same pass are
skipped without spending requests on a certain rejection.

**A subscription ending disables auto-posting** in the same transaction as the
status change: as a separate step after the mailing it would keep working for
as long as the mailing runs. Sources are kept — reading works without a
subscription, and someone returning a month later should not have to rebuild
their channel list.

**Shutdown.** On SIGTERM the scheduler is paused first, and only then are
running jobs awaited. The reverse order is unacceptable: the APScheduler
executor cancels running coroutines regardless of the `wait` flag, and a job
would be cut off mid-transaction.

## Trial period and multi-account protection

The trial is granted once and opens the Pro plan for `TRIAL_DAYS` days (a
subscription with status `trialing` and source `trial`). From there the same
worker handles it as it handles paid ones: it warns a day ahead and closes
access when the period ends.

There are three ways to bypass the grant, and each is closed by its own
mechanism:

| Bypass | Protection | Where it lives |
|---|---|---|
| Asking again from the same account | The `users.trial_activated_at` mark | `User.mark_trial_started` |
| A new account with the same phone | `UNIQUE(kind, fingerprint)` in `trial_claims` | `UserRepository.register_trial_fingerprints` |
| Two simultaneous taps | An advisory lock on `user_id` plus re-reading the row under it | `TrialService.activate` |
| A forwarded contact of someone else | Comparing `contact.user_id` with the message author | `tg_bot.handlers.trial._own_phone` |

**Why the phone and not the IP.** The Bot API exposes neither the user's
address nor a device identifier — the only attribute confirmed by Telegram
itself is the number sent through a `request_contact` button. The
`TrialFingerprintKind.IP` and `DEVICE` values are reserved for a web version
and a mini app.

**Why `contact.user_id` is verified.** A contact can be picked from the address
book and sent to the bot as an ordinary message. Such a contact arrives with
someone else's `user_id` or none at all, and checking the number alone would
allow activating trials with every acquaintance's number.

**What reaches the database.** Only HMAC-SHA256 of the normalised number with a
server-side secret. Plain SHA-256 is not enough: the entire space of Russian
numbers can be enumerated in minutes, and the hash would protect nothing.
Formatting is irrelevant — `+7 (900) 123-45-67` and `79001234567` produce the
same fingerprint.

**The order of operations** in `TrialService.activate` is chosen so that a
failure at any step cannot take away someone's right to a trial: state checks
first, then reserving the fingerprint, and only then the activation mark and
the subscription. It all runs in one transaction, and a rejection rolls back
the claimed fingerprint too — a rejected account may try again with a different
number.

**`TRIAL_FINGERPRINT_SECRET` is mandatory** when phone verification is on: the
application will not start without it. It must not be changed after launch —
previously computed fingerprints would stop matching and every user would
become eligible for a second trial.

Phone verification can be turned off with `TRIAL_REQUIRE_CONTACT=false`, but
then any new account gets the trial — a mode for local development, not for
production.

## Personal cabinet

`/cabinet` has four sections: sources, auto-posting channels, the word filter
and subscription status. Three of the setup flows are built on FSM because each
needs free-form input: a link, a forwarded message or a list of words does not
fit into `callback_data` — 64 bytes, and supplied by the client at that.

**Validation relies on an external source of truth, not on the shape of the
string.** A source is validated by trying to read the channel: a name like
`@channel` can be syntactically perfect and still belong to a private or
non-existent channel. A publication target is validated through
`get_chat_member` — the only way to learn whether the bot can post there is to
ask Telegram. An admin without posting rights is distinguished from a
non-admin: the two need different hints.

**A private channel is added by forwarding.** It has no username and cannot be
referenced any other way — `_extract_channel_reference` looks at
`forward_from_chat` first and only then at the text.

**The rights check is repeatable.** Rights are revoked as easily as they are
granted, so every target channel has a re-check button, and the result is
cached in `user_channels.bot_is_admin` to avoid asking Telegram before every
publication.

The owner is part of the query condition when deleting a channel or a word: the
id arrives in `callback_data`, and without the check someone else's row could
be deleted by guessing a number.

## Localisation

The interface is available in Russian, English and Ukrainian. Strings live in
`locales/<code>.json`; the catalogues are read at startup and kept in memory:
re-reading a file for every message is wasteful, and watching the disk for
changes is a source of races during a rollout.

**Why JSON and not Fluent or gettext.** The volume of text is measured in tens
of strings, and developers do the translating. Fluent would add one more
dependency and its own syntax for capabilities that are not needed here;
gettext would add a `.po` → `.mo` compilation cycle on every edit. JSON is
editable in any editor, and catalogue consistency is verified by a test: the
same set of keys and the same substitutions in every language.

**Plural forms.** The one thing a flat approach lacks. "1 day / 2 days / 5 days
left" is three forms in Russian and Ukrainian, two in English; substituting a
number without regard for the form reads like machine translation. The rules
follow CLDR and live in `select_plural_form`.

**Where the language comes from.** The order goes from the cheap source to the
expensive one: cache (Redis or process memory) → an already loaded user row →
a database query → the `language_code` hint from the Telegram client. The
person's choice always outranks the client hint: `users.language` is only set
when the row is created and changes solely through `/language`.

**A language switch** is saved to the database first and to the cache second.
The reverse order would leave the cache ahead of the database if the
transaction rolled back. The confirmation arrives already in the new language —
otherwise someone would press "English" and read the reply in Russian.

**Errors are translated too.** Billing and trial exceptions carry a translation
key rather than a ready-made phrase: the service layer knows nothing about
language, and the text inside the exception stays for the logs. Worker
notifications go out in the recipient's language — they did not choose the
moment of delivery, let alone expect it in Russian.

Redis being unavailable does not break localisation: the cache degrades into a
miss and the language is loaded from the database.

## Payments: Telegram Stars

Subscriptions are paid for with Stars — inside Telegram, without cards or an
external acquirer, so no `provider_token` is used. The plan catalogue lives in
`core/pricing.py`: prices take part in verifying the payment amount and must be
identical across all replicas at the moment of a rollout.

The payment path has three steps, and each of them can arrive more than once:

| Step | What happens | Protection |
|---|---|---|
| Invoice creation | A `payments` row in status `pending` with a TTL | A cap on unfinished invoices, `SingleFlightMiddleware` against a double tap |
| `PreCheckoutQuery` | Amount, currency, payer and deadline are verified; status → `processing` | The answer must fit into 10 seconds, so throttling is disabled for it |
| `SuccessfulPayment` | Payment confirmation and day granting | A row lock on the payment plus a unique `payment_id` in the subscription log |

`invoice_payload` comes from the client, so the `PreCheckoutQuery` step verifies
not only that the invoice exists but also the amount, the currency and the
payer — otherwise someone else's invoice could be used to pay for your own plan.

A subtlety of granting: if the subscription is created by this very payment,
the paid period is already built into its deadline and there is nothing to
extend — but the payment record is still written, otherwise a redelivered event
would extend the subscription.

## Link tracking

Outbound links in posts are replaced with short ones of the form
`{TRACKER_BASE_URL}/r/{code}`, clicks are counted, and the owner reads the
report with `/stats`. Without `TRACKER_BASE_URL` the rewriting is disabled: a
short link must point at a server reachable from the internet, and that cannot
be invented on the user's behalf.

**The redirect sits on the hot path** — a real person is following that link.
So the handler performs no PostgreSQL writes at all: the address comes from the
Redis cache, and the click is put into a queue that the `click_flush` background
job drains. A cache miss costs one database query and a warm-up.

**The answer is 307, not 301.** Browsers cache a permanent redirect forever:
the second click would never reach us, and the statistics would show one click
instead of a hundred. For the same reason the response is marked
`Cache-Control: no-store`.

**The buffer's price is honest:** losing Redis loses the clicks accumulated
since the last flush. For analytics that is acceptable — money or access cannot
be lost that way, a few clicks can. Events are *taken* from the queue rather
than read: re-flushing the same rows would double the counters.

**Unique clicks are counted by the database.** The log has
`UNIQUE(link_id, visitor_hash)`, and `ON CONFLICT DO NOTHING` answers "is this
visitor here for the first time?" without a separate check — the same
constraint also protects against re-flushing one batch.

**The visitor's address is not stored:** what goes into the database is an HMAC
of the "IP + User-Agent" pair with the same secret as the trial fingerprints.

**CTR is literally unavailable:** it needs a denominator — the number of
impressions — and Telegram does not report how many people saw a post in
someone else's channel. Instead the report shows the share of repeat clicks,
which answers the question "was this several people's interest or one person
clicking a lot?".

| Variable | Default | Purpose |
|---|---|---|
| `TRACKER_BASE_URL` | — | Public address of the redirect; empty disables rewriting |
| `TRACKER_LINK_TTL_DAYS` | `0` | Short link lifetime; 0 means unlimited |
| `WORKER_CLICK_FLUSH_INTERVAL` | `60` | How often clicks are moved into the database, seconds |

## Crypto payments: CryptoBot

A second payment method next to Stars. Enabled by the `CRYPTO_BOT_TOKEN`; without
it the section is simply not shown and the bot works as before — there is no
reason to require registration with a third-party service just to run locally.

**Idempotency is the same as for Stars, and that is not a coincidence.**
`confirm_payment` locks the payment row, `apply_payment_grant` relies on the
uniqueness of `payment_id` in the subscription log. Neither constraint depends
on the provider, so adding CryptoBot required no new mechanism against double
granting.

**The `payments` row is created before the provider is called.** The reverse
order would leave an invoice that exists in CryptoBot and not with us: a webhook
arriving for it would have nothing to attach to — money taken, nobody to credit.

**The webhook signature is verified before anything else.** This is the only
publicly reachable entry point into the application: without the check, anyone
could grant themselves a subscription by posting suitable JSON. The comparison
goes through `hmac.compare_digest` — a plain `==` stops at the first mismatched
byte, and the response time lets the signature be guessed character by
character.

**Webhooks always get a fast answer, and a 200 when possible.** CryptoBot
retries delivery until it receives a success, so answering a duplicate with an
error would mean an endless retry loop. Success is returned both for repeat
deliveries and for events that do not concern us.

**Only what is safe to retry is retried.** Invoice creation survives network
failures and 5xx responses; 4xx errors are not retried — the same request would
be rejected exactly the same way.

**An underpayment does not open access; an overpayment does:** the money has
already been transferred, and returning it is harder than delivering what was
paid for.

| Variable | Default | Purpose |
|---|---|---|
| `CRYPTO_BOT_TOKEN` | — | Crypto Pay application token; empty disables the method |
| `CRYPTO_BOT_API_URL` | `https://pay.crypt.bot/api` | API address (testnet has its own) |
| `CRYPTO_BOT_WEBHOOK_PATH` | `/webhook/cryptobot` | Receiver path; must match the application settings |
| `CRYPTO_BOT_WEBHOOK_HOST` / `PORT` | `0.0.0.0` / `8080` | Where webhooks are listened for |
| `CRYPTO_BOT_INVOICE_TTL_MINUTES` | `60` | Crypto invoice lifetime |
| `CRYPTO_BOT_TIMEOUT` | `15` | API call timeout, seconds |

## Growth: referrals and promo codes

Both mechanisms end in the same thing — granting subscription days — and both
have to survive a repeated tap. Someone who sees no immediate answer presses the
button again; Telegram redelivers the update; there may be several bot replicas.
Exactly-once therefore rests on unique indexes rather than on checks in code:

* `referrals.referred_id` — an invitee is counted exactly once, ever, no matter
  how many links they arrived through;
* `promocode_redemptions (promocode_id, user_id)` — one code per person.

The order of operations follows directly from that: insert the key row first
(`INSERT ... ON CONFLICT DO NOTHING RETURNING`), and only if it succeeded, grant
the reward — in the same transaction. A separate `SELECT` asking "have we
granted this already?" would leave a window for a second concurrent request to
slip through.

**The referral link.** There is one entry point — `/start ref_<CODE>`; Telegram
offers no other way to pass a payload. Both sides get their bonus immediately on
arrival, and its size is set by `REFERRAL_BONUS_DAYS` (3 days by default).

The price of instant granting is vulnerability to farming with empty accounts.
It is held back by the same fingerprints that protect the trial, but the
question is only fully closed by a "bonus after the first payment" policy. That
policy is already expressed in the model: the `qualified` state and the
`Referral.qualify(payment_id, ...)` method exist and work, and switching over
needs neither a migration nor a schema change — it is enough to move the
`reward()` call from `/start` into the successful-payment handler.

The inviter cannot be changed after the fact: the `referred_by_id IS NULL`
condition is part of the `UPDATE` itself rather than a check in code, so two
simultaneous arrivals through different links cannot overwrite each other's
"parent".

**Promo codes.** Redemption is `/promo <CODE>` or a button in the cabinet. The
code row is locked for writing (`SELECT ... FOR UPDATE`) for the duration of the
check: without it, two simultaneous redemptions of the last remaining code would
read the same counter and both consider the limit unmet. Going over the limit is
additionally prevented by `CHECK (activations <= max_activations)` — even a bug
in application code cannot turn a code into an infinite one.

Rejection reasons differ on purpose. "The code is used up", "it has expired" and
"you already used this code" are different news: in the first case the person
will go looking for another code, in the third that is pointless. The case where
the code was exhausted by the very person asking is handled separately: telling
them "the code is gone" would be wrong.

Discount codes (`discount_percent`) and codes tied to a plan are not redeemed by
a standalone command: neither percentages nor "days of this plan" are defined
outside a payment. Such codes are applied at checkout.

Codes are created by an administrator: `/newpromo <days> [limit] [note]`. The
code itself is generated at random from an alphabet without visually ambiguous
characters — hand-invented codes collide with each other sooner or later.

## Admin panel and broadcasts

Rights are checked by the `IsAdmin` filter attached to the whole router rather
than by the first line of every handler. The difference is not cosmetic: an
update that fails the filter is not considered handled at all, so to an outsider
`/admin` is indistinguishable from any other unknown text — the bot simply does
not reply. A check inside the handler, by contrast, would reveal that the
command exists. A router-level filter also makes it impossible to forget about
rights when adding a new handler here.

There are two sources of rights. The `users.is_admin` flag is the working one:
granted and revoked on the fly. The `ADMIN_IDS` list in the environment is the
bootstrap one: someone has to set the first flag in the database, and only
someone who already has rights can do that through the bot.

**Metrics** (`/admin`) are collected as a single snapshot: user counters in one
pass over the table instead of five, the subscription breakdown by grouping,
revenue as an aggregate over successful payments.

Two caveats matter when reading the panel:

* "Revenue over 30 days" is the sum of successful payments, not MRR. The
  subscription is sold as one-off periods, there is no auto-renewal, and there
  is nothing to compute recurring revenue from. With a noticeable share of
  annual payments the number jumps.
* Currencies are not added together. Stars and USDT are different units, there
  is no exchange rate between them in the database, and a "total" made of them
  would simply be wrong; the panel shows them separately.

Conversion is computed against two denominators at once: the share of payers
among everyone registered (understated — the denominator includes those who
arrived a minute ago) and among those who reached the trial (a fairer funnel).
The numerator is always the number of *payers*, not of payments: one person with
five renewals is still one paying customer.

**Broadcasts** are built around three constraints.

*The Bot API limit* — about thirty messages per second for the bot as a whole.
Exceeding it earns flood control against the entire bot, not just the broadcast,
replies in private chats included. So tokens are taken from the same bucket as
worker notifications and publications: a dedicated bucket for broadcasts would
mean nothing limits the combined rate. The speed is set by `BROADCAST_RATE` (25
by default — headroom for everything else being sent).

*The size of the user base.* Recipients are read in pages by an `id > after_id`
cursor rather than in one query or through `OFFSET`: a broadcast over a large
base runs for minutes, new users appear meanwhile, and an offset would start
skipping and repeating rows. The database connection is returned to the pool
between pages.

*Unreachable recipients.* A 403 and "chat not found" mean writing to this person
is pointless forever. Such recipients are flagged in `users.is_bot_blocked` in
batches and drop out of subsequent broadcasts — otherwise every new broadcast
would spend shared-limit tokens on them. One subtlety: Telegram returns "chat
not found" with code 400 rather than 404, so `TelegramNotFound` does not fire
here and the condition is detected in the error text.

The message is not rebuilt but copied (`copy_message`) from what the
administrator sent to the bot: media, captions and formatting carry over as they
are. Rebuilding on our side would require a separate branch per attachment type
and would lose the formatting on the first non-standard case. A copy rather than
a forward — a forwarded message shows its source, which is the administrator's
private chat. Buttons are declared as lines of `Text | https://example.com`.

There are three audiences: everyone, holders of an active paid subscription, and
people whose trial expired. The last one is the group broadcasts usually exist
for: someone tried the product and left. Selection goes by deadlines rather than
by status alone, because the worker does not mark subscriptions expired
instantly, and between expiry and its pass a person must not land in "active".

A broadcast runs as a background job: the update cannot be held open for the
whole send — a database transaction would be held with it. Progress is shown in
an edited message, and stopping is graceful: sends already started are finished,
new ones are not begun. The state lives in process memory, so restarting the bot
interrupts a broadcast and it cannot be resumed from the middle; a table with a
cursor would only pay off on a genuinely large user base.

The panel's interface is in Russian, unlike the rest of the bot: it is seen by
the service operator, not by a customer, and translating it into three languages
for screens one person opens is pointless. Everything that reaches an end user —
the referral bonus message, the promo code result — is localised as usual.

## Rate limiting and anti-spam

The limiter uses a token bucket: it allows short bursts (a person tapping a
button a few times in a row) while holding the average rate. All of the bucket's
arithmetic runs as a single Lua script on the Redis side — the sequence "read,
compute, write" as separate commands is not atomic, and two concurrent requests
would exceed the limit under exactly the conditions the limit exists for.

| Layer of protection | What it does |
|---|---|
| `ThrottlingMiddleware` | A shared limit on messages and on taps (separate buckets) |
| The `rate_limit(...)` flag | A per-handler limit |
| `SingleFlightMiddleware` | Prevents one tap from being handled twice concurrently |
| `AntiFloodPolicy` | Escalation: systematic flooding → a temporary mute of growing length |

A per-handler limit is declared next to the handler:

```python
@router.callback_query(RefreshCB.filter(), **rate_limit(3, 60, scope="refresh_button"))
async def refresh_channel(...): ...
```

Flags are read only by the **inner** middleware: the outer one runs before the
dispatcher has picked a handler and knows nothing of its flags.

If Redis is unavailable the limiter is not switched off but temporarily falls
back to in-process counters: flood protection stops being shared across replicas
but keeps working. After a series of errors, calls to Redis pause for 15 seconds
so that every request does not pay a timeout.

Without `REDIS_URL` the bot starts on local storages — acceptable for a single
instance but not for several replicas: each would keep its own counters.

### The security chain

There are four layers, and the order between them is not arbitrary — it is
assembled in one place, in `setup_security()` (`tg_bot/middlewares/security.py`).

**1. Request budget.** A token bucket: how many operations per unit of time,
with escalating penalties for systematic flooding. It comes first not for
cost reasons but because the anti-flood policy must see every attempt: a layer
rejecting requests earlier would keep it from ever reaching the mute, and a
flooder would get an eternal "too often" instead.

**2. A hard interval.** A sliding window over a Redis sorted set: no more than
one request every `SECURITY_COOLDOWN` seconds. It is needed because the bucket
deliberately allows a burst — at a limit of "20 per minute" all twenty messages
can pass within one second. That is usually convenient, but expensive handlers
get a lot done before the budget runs out. The window imposes a strict floor
regardless of the remaining budget.

Marks are stored with a unique member per request. That is not an
implementation detail but a condition of correctness: a shared set member would
make `ZADD` overwrite instead of adding, and the window would count one request
instead of ten.

**3. Single flight.** A lock for the duration of handling a tap, keyed by the
"user + button payload" pair. Released right after the handler.

**4. Critical actions.** Things that must not be performed twice by accident:
issuing an invoice, activating the trial. They are marked with the
`critical("name")` flag, and handlers sharing a name share the protection.

The difference from single flight is substantial. There, the danger is a
concurrent repeat; here it is also a repeat *right after success*: the handler
finished in a second, the person tapped again two seconds later, the lock is
already released — and they got a second invoice. Hence two keys: a lock for the
duration of the work (its TTL is insurance against a dead process, not a working
parameter) and a cooldown that is set only after success and expires on its own.
After an exception no cooldown is set: a failed attempt should be repeatable
immediately, not punished for someone else's failure.

About redlock. The classic algorithm assumes several independent Redis masters
and a quorum between them. Here there is a single master, and porting redlock
over would mean imitating its guarantees without having them. With one master
the correct primitive is `SET NX PX` with an owner check on release, and that is
what is used. Its boundary: a lock without a fencing token, which another process
can seize if this one stalls for longer than the TTL. So it never serves as the
only protection — final exactly-once comes from database constraints (the unique
payment idempotency key, the cap on unfinished invoices), while this layer
removes the vast majority of cases cheaply and before reaching the database.

When the store is unavailable, protection of critical actions **lets the request
through** by default rather than rejecting it: rejecting would mean payments do
not work, whereas a duplicate invoice is cut off by the unique key in the
database anyway. The opposite behaviour is enabled with
`SECURITY_FAIL_CLOSED=true`.

The database session is not part of this chain: its middleware is the outer one
and must also cover the filters, which need an open transaction too. The
transaction boundary is the handling of an update as a whole — commit after a
successful return from the handler, rollback on any exception.

## Repository layer

`BaseRepository[T]` provides the shared operations — `get_by_id`, `get_all`,
`update`, `add`, `delete`, `exists`, `count`, `get_for_update`. Subclasses add
the queries of their own area, and all SQL lives here and nowhere else.

`update` performs `UPDATE ... RETURNING` in a single query. The "load the object,
change attributes, save" sequence would cost two round trips to the database and
leave a window between them in which a neighbouring transaction can modify the
row. Where "read — modify — write" is genuinely needed, `get_for_update` is used.

Relationships are declared with `lazy="raise"`, so touching them outside a
session fails — and rightly so: implicit loading in async code yields either an
extra query per access or a crash outside the context. Where a relationship is
needed it is loaded explicitly: `UserRepository.get_with_subscription` uses
`selectinload` rather than `joinedload`, so the user row is not multiplied by the
number of their subscriptions.

Driver errors do not leak out: the `handle_db_errors` decorator translates
`IntegrityError` into `ConflictError`, server-cancelled transactions into
`ConcurrencyError` and everything else into `RepositoryError`. Application code
catches the layer's exceptions, not `SQLAlchemyError`.

## Transactions and idempotency

Repositories never call `commit`: the transaction boundary is set by
`UnitOfWork`, so one application operation is either applied in full or not at
all.

```python
async with uow_factory() as uow:
    result = await uow.subscriptions.apply_payment_grant(
        payment_id=payment.id, subscription_id=sub.id, user_id=user.id, days=30,
    )
    await uow.commit()
```

Leaving the block without `commit` rolls the changes back — a forgotten commit
never results in a partial write.

Mechanisms against races:

| Problem | Technique |
|---|---|
| A repeated `/start`, a double tap on "Pay" | `INSERT … ON CONFLICT … RETURNING` — the database decides, with no window between check and insert |
| A redelivered `successful_payment` | A row lock on the invoice, `SELECT … FOR UPDATE` |
| Granting days twice | Inserting the event with `ON CONFLICT (payment_id) DO NOTHING` as the decision point |
| Extending a subscription | Date arithmetic as a SQL expression (`GREATEST(expires_at, now()) + interval`) instead of reading into Python |
| Creating a subscription that does not exist yet | An advisory lock on the user: there is nothing to lock `FOR UPDATE` while the row does not exist |
| Several workers over one queue | `FOR UPDATE SKIP LOCKED` plus the mark in the same query |
| Deadlocks | `UnitOfWorkFactory.transaction` retries the whole transaction with exponential backoff |

## Migrations

The schema is versioned by Alembic; the application never changes it.

```bash
alembic upgrade head                            # apply
alembic downgrade -1                            # roll back one step
alembic revision --autogenerate -m "description"  # generate after editing models
alembic check                                   # verify models and DB agree
```

In Docker, migrations are applied automatically before the bot starts (the
`command` of the `bot` service).

## Running with Docker

```bash
cp .env.example .env          # set BOT_TOKEN and the database password
docker compose -f Docker/docker-compose.yml up -d --build
docker compose -f Docker/docker-compose.yml logs -f bot worker
```

## Running locally

A running PostgreSQL is required. Alembic applies migrations but **does not
create the database itself** — that has to be done once by hand.

```bash
python -m venv .venv
. .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                  # set BOT_TOKEN, set DB_HOST=localhost

createdb -U postgres news_db          # or: psql -U postgres -c "CREATE DATABASE news_db"
alembic upgrade head                  # apply the schema

python main.py                        # the bot
python -m worker                      # the worker — in a separate terminal
```

If `createdb` and `psql` are not on the `PATH` (typical for a Windows install
where PostgreSQL runs as a service), the database can be created with the
project's own tools — `asyncpg` is already installed:

```bash
python - <<'EOF'
import asyncio, asyncpg
async def main():
    conn = await asyncpg.connect(user="postgres", password="postgres",
                                 host="localhost", database="postgres")
    await conn.execute('CREATE DATABASE "news_db"')
    await conn.close()
asyncio.run(main())
EOF
```

The bot and the worker are two independent processes: the worker sends
subscription expiry notices and expires unpaid invoices. Without it the bot
works, but subscriptions will not be closed when their time comes.

At startup both processes check the database and, if it is unavailable or the
schema is not applied, exit with code `3` and a clear message instead of failing
later on the first user request.

**Only one bot instance may poll Telegram at a time.** A second one gets
`TelegramConflictError: terminated by other getUpdates request` — including when
a Docker container is up in parallel, or a forgotten process from the previous
run.

## Tests

```bash
pip install -r requirements-dev.txt
pytest                                # the whole suite
pytest -m "not db"                    # without tests that need PostgreSQL
```

Data-layer tests run against a separate `news_db_test` database (created
automatically). If PostgreSQL is unavailable they are skipped with an
explanation rather than failing. The test database address is set through
`TEST_DATABASE_URL` or `TEST_DB_HOST` / `TEST_DB_PORT` / `TEST_DB_USER` /
`TEST_DB_PASS` / `TEST_DB_NAME`.

### How isolation works

Changes are **really committed**, and tables are cleared with `TRUNCATE` after
the test. The familiar trick of wrapping a test in an outer transaction and
rolling it back does not apply here: `UnitOfWork` and the background jobs open
transactions themselves and commit them themselves. Handing them someone else's
session means testing code other than the one that runs in production — the
`commit()` at the update boundary, the `FOR UPDATE SKIP LOCKED` in worker queues
and the advisory locks would all disappear. An attempt to keep the rollback with
nested savepoints fell apart on their out-of-order release.

The price of that decision: tests cannot be run in parallel against one database,
and `TRUNCATE` waits no longer than five seconds for the tables to be free — an
unclosed transaction in a test must produce a clear error rather than an endlessly
hanging run.

The test's session and the session of the code under test are **different**. That
is not a detail: an object loaded by one session does not see changes made by
another, and asserting through that same object would show stale state. So data
factories write through `db_session` while the result is re-read through `uow`.

### What the suite covers

| File | What it verifies |
|---|---|
| `test_repositories.py` | CRUD, relationship preloading, payment uniqueness constraints |
| `test_billing.py` | The three steps of paying with Stars and idempotency of a repeated event |
| `test_workers.py` | Subscription expiry, warnings and invoice cleanup under `freezegun` |
| `test_security_middleware.py` | The sliding window, the hard interval, locks on critical actions |
| `test_broadcaster.py` | Audience selection, the outgoing rate limit, unreachable recipients |
| `test_growth_services.py` | Referrals and promo codes: granting and protection against repeats |
| `test_admin_panel.py` | Access rights and the arithmetic of business metrics |
| `test_crypto.py`, `test_webhook.py` | Crypto payments and webhook handling |
| `test_publisher.py`, `test_tracker.py`, `test_search.py`, `test_dedup_index.py` | Auto-posting, link tracking, search, deduplication |
| `test_conftest_smoke.py` | The harness itself: fixtures and time freezing |

Time is frozen with `freezegun` and `real_asyncio=True`. The flag is mandatory:
freezegun replaces `time.monotonic`, on which the event loop's timers are built,
and without it any `await asyncio.sleep()` inside a test would hang forever. The
freeze does not extend to PostgreSQL — where the server makes the decision, the
point in time is passed into the query explicitly.

The limiter's Redis backend is tested against `fakeredis` (installed from
`requirements-dev.txt`); to run the same tests against a real Redis, set
`TEST_REDIS_URL`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | — (required) | Bot token from @BotFather |
| `DB_USER` / `DB_PASS` / `DB_NAME` | `postgres` / `postgres` / `news_db` | PostgreSQL credentials |
| `DB_HOST` / `DB_PORT` | `db` / `5432` | Database address. Outside Docker use `localhost`; in compose the value is overridden with `db` |
| `LOG_LEVEL` | `INFO` | Logging level |
| `DISPLAY_TZ` | `UTC` | Timezone used to display dates |
| `REDIS_URL` | — | Redis for limits and FSM; empty means in-memory storages |
| `RATE_LIMIT_ENABLED` | `true` | Enables rate limiting |
| `RL_MESSAGE_LIMIT` / `RL_MESSAGE_WINDOW` | `20` / `60` | Message limit |
| `RL_CALLBACK_LIMIT` / `RL_CALLBACK_WINDOW` | `30` / `60` | Button tap limit |
| `RL_MUTE_DURATIONS` | `30,120,600` | Mute lengths by violation number |
| `SECURITY_COOLDOWN` | `0.5` | Minimum interval between requests, seconds |
| `SECURITY_LOCK_TTL` | `30` | Lifetime of a critical-action lock, seconds |
| `SECURITY_ACTION_COOLDOWN` | `5` | Pause after a successful critical action, seconds |
| `SECURITY_FAIL_CLOSED` | `false` | Reject critical actions when Redis is unavailable |
| `INVOICE_TTL_MINUTES` | `15` | How long an issued invoice stays valid |
| `MAX_PENDING_INVOICES` | `3` | Cap on unfinished invoices per user |
| `TRIAL_ENABLED` | `true` | Trial period granting |
| `TRIAL_DAYS` | `7` | Trial length, days |
| `TRIAL_REQUIRE_CONTACT` | `true` | Require a verified phone number |
| `TRIAL_FINGERPRINT_SECRET` | — (required with phone verification) | HMAC secret for fingerprints |
| `ADMIN_IDS` | — | Comma-separated Telegram IDs of administrators; the bootstrap list |
| `REFERRAL_BONUS_DAYS` | `3` | Days each side gets for an invitation |
| `BROADCAST_RATE` | `25` | Messages per second; the bot's shared outgoing limit |
| `BROADCAST_WORKERS` | `8` | How many sends run concurrently |
| `BROADCAST_PAGE_SIZE` | `500` | Recipient page size |
| `WORKER_EXPIRY_NOTICE_HOURS` | `24` | How many hours ahead to warn about expiry |
| `WORKER_EXPIRY_INTERVAL` | `900` | How often to look for expiring subscriptions, seconds |
| `WORKER_EXPIRATION_INTERVAL` | `300` | How often to revoke access, seconds |
| `WORKER_PUBLISH_INTERVAL` | `30` | How often to drain the publication queue, seconds |
| `WORKER_INVOICE_INTERVAL` | `600` | How often to expire unpaid invoices, seconds |
| `WORKER_BATCH_SIZE` | `100` | Batch size per pass |
| `DEDUP_ENABLED` | `true` | Collapsing repeated news |
| `DEDUP_HAMMING_THRESHOLD` | `16` | Simhash candidate selection threshold |
| `DEDUP_SIMILARITY_THRESHOLD` | `0.75` | Jaccard confirmation threshold |
| `DEDUP_LOOKBACK_HOURS` | `48` | How far back to look for the original |
| `PARSE_COOLDOWN` | `60` | Pause between refreshes of one channel, seconds |
| `MAX_POSTS` | `10` | How many posts to show |
| `REQUEST_TIMEOUT` / `MEDIA_TIMEOUT` | `15` / `30` | HTTP timeouts, seconds |
| `MAX_MEDIA_BYTES` | `20971520` | Size limit for one attachment |
| `DB_ECHO` | `false` | Print SQL to the log |

## Operational notes

* Alembic does not track PostgreSQL ENUM types automatically: when adding a new
  value to an enum, edit the migration by hand (`ALTER TYPE ... ADD VALUE`).
* Without `REDIS_URL`, rate limits and FSM state live in process memory: fine for
  a single instance, but with several replicas the effective limit becomes a
  multiple of the declared one and state is lost on restart. Configure Redis for
  production.
* The bot only works with channels from the allowlist in `core/config.py` —
  arbitrary addresses from callback_data are not accepted.

---

<p align="center">
  <a href="https://t.me/dekelia_bot">@dekelia_bot</a> ·
  <a href="https://github.com/vindibee/Telegram-News-Aggregator">Source on GitHub</a>
</p>
