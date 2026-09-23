# bzapper

Official **Python SDK** for the [bZapper](https://bzapper.com.br) WhatsApp gateway API — a multi-tenant WhatsApp gateway: connect numbers, send and receive messages, rotate numbers (anti-ban) and track usage.

Zero runtime dependencies (pure standard library). Python 3.9+.

## Install

```bash
pip install bzapper==0.8.0
```

**Pin the exact version** (`bzapper==X.Y.Z` in `requirements.txt` / `pyproject.toml`).
Every release note states whether it changes the public surface (a signature or
return shape) or is purely additive — upgrade on purpose, not by accident.

## Hello world

```python
from bzapper import Client

client = Client("bz_live_...")
client.send_text("+5511999999999", "Hello from bZapper!")
```

The base URL defaults to `https://api.bzapper.com.br` and is optional — pass one only
for dev/self-host: `Client("bz_live_...", "http://localhost:8080")`.

## Client configuration

```python
from bzapper import Client

client = Client(
    api_key="bz_live_...",                    # tenant API key
    base_url="http://localhost:8080",          # optional, defaults to prod (dev/self-host only)
    locale="pt-BR",                            # optional, sets Accept-Language
    timeout=30,                                # optional, seconds per attempt
    max_retries=2,                             # optional, automatic retries (0 disables)
    project_id="proj_...",                     # optional, sent as X-Project-Id
)
```

## Authentication

Create the API key in the bZapper panel or with
`client.create_key(...)`. The key is sent as `Authorization: Bearer <api_key>`
and belongs to a **project** — numbers, inbox, keys and stats are isolated per
project. An account-level key can pick a project per client with
`project_id=` (the `X-Project-Id` header).

Every request also sends `Accept: application/json`, `X-Bzapper-Client:
bzapper-python/<version>` (the same value as `User-Agent`), a per-call
`X-Request-Id` and — on writes — an `Idempotency-Key`; `Content-Type:
application/json` only when there is a JSON body, and `Accept-Language` when
`locale` is set.

## Messages

Every message method accepts the common **SendBase** options as keyword
arguments: `instance_id`, `pool_id`, `quoted_message_id`, `quoted_participant`
(author of the quoted/reacted message — only needed in groups when it isn't in
bZapper history), `client_reference`, `mentions` (JIDs or plain phones),
`sticky`, `scheduled_at` (RFC3339 — schedules the send), `groups`/`tags`
(contact-group/tag keys to target) and `force`.
Each returns the queued-message object
(`message_id`, `status`, optional `client_reference`).

Safe retries: the SDK already sends an automatic `Idempotency-Key` on every
write and repeats it on its own retries. Pass `idempotency_key="..."` (up to 255
chars, sent verbatim) to any write to make YOUR retries safe too. Repeating it within 24h returns the SAME
response without sending twice (`409 idempotency_in_progress` while the first
is still running; `422 idempotency_key_reused` if the body changed).

```python
client.send_text("+5511999999999", "Order #42 confirmed", idempotency_key="order-42")
```

`to` is a phone in E.164 (`+5511999999999`) or a JID.

```python
# Text
client.send_text("+5511999999999", "Hello!")

# Verification code (OTP): context text + the code in its own bubble; 1 send.
# The code is never stored or shown by bZapper.
client.send_otp("+5511999999999", "482913", expiry_minutes=10)

# Scheduled send + scheduled list/cancel
s = client.send_text("+5511999999999", "Reminder", scheduled_at="2026-10-01T12:00:00Z")
client.list_scheduled(limit=20)
client.cancel_scheduled(s["scheduled_id"])

# Image (use url OR base64, never both)
client.send_image("+5511999999999", {"url": "https://picsum.photos/600", "caption": "Hi"})

# Video
client.send_video("+5511999999999", {"url": "https://example.com/clip.mp4"})

# Document
client.send_document("+5511999999999", {"url": "https://example.com/file.pdf", "filename": "file.pdf"})

# Audio — set ptt=True for a voice note
client.send_audio("+5511999999999", {"url": "https://example.com/note.ogg", "ptt": True})

# Sticker
client.send_sticker("+5511999999999", {"url": "https://example.com/sticker.webp"})

# Location
client.send_location("+5511999999999", -23.5613, -46.6565, name="Av. Paulista", address="São Paulo")

# Contact
client.send_contact("+5511999999999", contact_name="Berni Software")

# Poll
client.send_poll("+5511999999999", "Pizza or sushi?", ["Pizza", "Sushi"], selectable_count=1)

# Reaction (quoted_message_id is the wa_message_id; empty emoji removes it)
client.send_reaction("+5511999999999", quoted_message_id="ABCD1234", emoji="👍")

# Buttons
client.send_buttons(
    "+5511999999999",
    "Choose an option:",
    [{"id": "a", "title": "Option A"}, {"id": "b", "title": "Option B"}],
    footer="Powered by bZapper",
)

# List
client.send_list(
    "+5511999999999",
    "Pick from the menu:",
    [{
        "title": "Drinks",
        "rows": [
            {"id": "1", "title": "Coffee", "description": "Hot"},
            {"id": "2", "title": "Tea"},
        ],
    }],
    button_text="Open menu",
)
```

### MediaInput

The `media` argument is a dict: `{"url"?, "base64"?, "caption"?, "filename"?, "mimetype"?, "ptt"?}`. Use **`url` OR `base64`, never both**.

### Caveat: buttons & lists

Buttons and lists are **not reliable** on WhatsApp (worse in groups). The API
**always** also sends an equivalent **numbered text menu** as a fallback. Design
your flows so the numbered menu alone is enough.

## Instances (numbers)

```python
client.list_instances()
inst = client.create_instance("+5511999999999", nickname="Support", proxy_url=None)
client.get_instance(inst["id"])

# Connect via QR (default) or pairing code
res = client.connect_instance(inst["id"], method="qr")    # -> {"status", "qr_code"?}
res = client.connect_instance(inst["id"], method="code")  # -> {"status", "pair_code"?}

client.disconnect_instance(inst["id"])
client.logout_instance(inst["id"])         # next connect needs a new QR
client.clear_instance_session(inst["id"])  # stuck pairing: wipe the device, re-pair

# Update the WhatsApp profile (display name / about / picture)
client.set_profile(inst["id"], display_name="Support", status_message="We reply fast")
client.set_privacy(inst["id"], "last", "contacts")

# Network, inbound filters, lifecycle
client.set_instance_proxy(inst["id"], "http://user:pass@proxy:3128")
client.set_inbound_filters(inst["id"], ignore_groups=True, ignore_status=True)
client.archive_instance(inst["id"])      # keeps history; list with list_instances(archived=True)
client.unarchive_instance(inst["id"])
client.delete_instance(inst["id"])

# Official WhatsApp Business (Cloud API) — projects created with api_mode="OFFICIAL"
client.get_official_account()
client.connect_official_account("waba_id", "phone_number_id", "access_token")
client.disconnect_official_account()
```

## Pools (number rotation)

```python
pool = client.create_pool(name="Sales", strategy="health_weighted", is_default=True)
client.add_pool_number(pool["id"], inst["id"])
client.list_pools()
client.get_pool(pool["id"])
client.send_text("+5511999999999", "Hi", pool_id=pool["id"])  # rotates across the pool
```

## Groups, presence and conversations

For these advanced calls `instance_id` is **required**. It travels in the query
string for groups/conversations and in the body for presence/chats/contacts —
the SDK handles that for you, you just pass it as an argument. `jid` is the
group/chat JID.

```python
inst_id = "01J..."  # an instance id

# Presence — works in groups too! Use the group JID as `to`.
client.presence_chat(inst_id, "+5511999999999", "typing")
client.presence_chat(inst_id, "12036304@g.us", "typing")    # group presence
client.presence_chat(inst_id, "12036304@g.us", "paused")

# Conversations
client.list_conversations(inst_id)
client.conversation_history(
    "12036304@g.us", inst_id, before="2026-06-01T00:00:00Z", limit=50  # limit ≤ 200
)

# Chats — archive / pin / mark read (on=True) or undo (on=False)
client.archive_chat("12036304@g.us", inst_id, on=True)
client.pin_chat("12036304@g.us", inst_id, on=True)
client.mark_chat("12036304@g.us", inst_id, on=True)
client.mute_chat("12036304@g.us", inst_id, on=True)

# Edit / revoke / forward / read receipts
client.edit_message("message_id", "Fixed text")
client.revoke_message("message_id", for_everyone=True)
client.forward_message(inst_id, "+5511888888888", "5511999999999@s.whatsapp.net", "wamid...")
client.mark_read("wamid...", inst_id, "5511999999999@s.whatsapp.net")

# Labels (experimental), block list and calls
label = client.create_label(inst_id, "VIP", color="1")
client.apply_chat_label("5511999999999@s.whatsapp.net", inst_id, label["id"])
client.list_labels(inst_id)
client.delete_label(label["id"], inst_id)
client.block_contact("5511999999999@s.whatsapp.net", inst_id)
client.get_blocklist(inst_id)
client.unblock_contact("5511999999999@s.whatsapp.net", inst_id)
client.reject_call(inst_id, "5511999999999@s.whatsapp.net", "call_id")

# Groups
client.list_groups(inst_id)
group = client.create_group(inst_id, "My group", ["+5511999999999", "+5511888888888"])
client.get_group(group["jid"], inst_id)
client.update_group_participants(
    group["jid"], inst_id, "add", ["+5511777777777"]  # add|remove|promote|demote
)
client.group_invite(group["jid"], inst_id)          # -> invite link/code
client.preview_group_invite(inst_id, "Cabc123InviteCode")  # name/size WITHOUT joining
client.join_group(inst_id, "Cabc123InviteCode")     # join via invite code
client.update_group(group["jid"], inst_id, name="New name", announce=True)
client.group_invite(group["jid"], inst_id, reset=True)  # revoke the old link
client.list_join_requests(group["jid"], inst_id)
client.update_join_requests(group["jid"], inst_id, ["+5511777777777"], approve=True)
client.leave_group(group["jid"], inst_id)

# Contacts — which numbers are on WhatsApp?
client.contacts_check(inst_id, ["+5511999999999", "+5511888888888"])
```

## Contacts (CRM)

The contact base is shared across the account and fed automatically by your
conversations (the contact ↔ number/project link is kept by the API).

```python
c = client.create_contact("+5511999999999", name="Ana", email="ana@example.com")
client.update_contact(c["id"], document="123.456.789-00")
client.list_contacts(tags=["vip"], tags_match="all", has_email=True, sort="name", limit=50)
client.get_contact(c["id"])
client.get_contact_history(c["id"], limit=20)
client.add_contact_note(c["id"], "Asked for a quote")

client.create_tag("vip", name="VIP", color="#f59e0b")
client.mutate_contact_tags(c["id"], add=["vip"], remove=["lead"])
client.create_contact_group("clients", name="Clients")
client.mutate_contact_groups(c["id"], add=["clients"])

client.opt_out_contact(c["id"])            # LGPD opt-out: never receives sends
client.opt_in_contact(c["id"])
client.create_suppression("+5511888888888", reason="complaint")
client.list_suppressions(limit=100)
client.delete_suppression("+5511888888888")
client.delete_contact(c["id"])
```

### Bulk import

Up to **1000 rows per call**, upserted by phone. A new contact comes in as
`source: import` / `status: pending_validation` (it still needs opt-in before a
campaign); an existing one has only the fields you sent updated — a blank value
never erases what is there. Tags and groups are created on demand. A bad row is
reported and **does not** fail the rest of the call: `errors` carries
`phone_required`, `invalid_phone`, `invalid_email`, `write_failed` or
`taxonomy_failed`, while `skipped_rows` carries `duplicate_phone`, `suppressed`,
`opted_out`, `blocked`, `unreachable` and `deleted` (an opted-out contact is
never resurrected). Use `dry_run=True` to validate without writing anything.

```python
result = client.import_contacts(
    [
        {"phone": "+5511999999999", "name": "Ana", "email": "ana@example.com",
         "tags": ["lead"], "groups": ["clients"]},
        {"phone": "+5511888888888", "name": "Bar do Zé",
         "document": "12.345.678/0001-90", "document_type": "cnpj",
         "address": {"street": "Av. Paulista", "number": "1000",
                     "city": "São Paulo", "state": "SP", "zip": "01310-100",
                     "country": "BR"}},
    ],
    dry_run=True,   # try it dry first, then run it for real
)
print(result["total"], result["created"], result["updated"], result["skipped"])
for row in result.get("errors", []) + result.get("skipped_rows", []):
    print(row["index"], row["phone"], row["reason"], row.get("detail"))
```

### CSV export

`export_contacts()` is the one endpoint that does **not** answer JSON: it returns
the CSV itself, as a `str` (UTF-8 decoded, BOM stripped, quoting untouched). It
takes the same filters as `list_contacts` (no `offset`), columns are
`phone,name,email,status,source,tags,groups,created_at,last_activity_at`, tags and
groups come `;`-joined and timestamps are RFC 3339 UTC. The API streams the file;
cap it with `limit` (max `100000`) and export a big base in slices.

```python
csv_text = client.export_contacts(tags=["vip"], status="active", sort="name", limit=5000)

with open("contacts.csv", "w", encoding="utf-8", newline="") as fh:
    fh.write(csv_text)

# Or read it straight from memory with the stdlib:
import csv, io
for row in csv.DictReader(io.StringIO(csv_text)):
    print(row["phone"], row["name"], row["tags"])
```

## Campaigns

```python
camp = client.create_campaign(
    [{"body": "Hi {name}! {Offer|Deal} of the week: ..."}],
    name="Week 38", pool_id=pool["id"], pacing_profile="conservative",
)
client.add_campaign_recipients(camp["id"], contact_filter={"tags": ["vip"]})
client.estimate_campaign(recipients=500, pacing="conservative")
client.get_campaign_eligibility(pool_id=pool["id"])
client.dry_run_campaign(camp["id"])
client.start_campaign(camp["id"])
client.pause_campaign(camp["id"]); client.resume_campaign(camp["id"])
client.list_campaign_recipients(camp["id"], limit=100)

header = client.upload_campaign_media("banner.png")  # bytes, path or binary file
```

## Projects, brand and users

```python
proj = client.create_project("Store B", api_mode="UNOFFICIAL")  # api_mode is immutable
client.update_project(proj["id"], "Store B (SP)", color="#0ea5e9")
client.get_projects_health()
client.set_project_brand(proj["id"], {"display_name": "Store B", "about": "..."})
client.upload_project_logo(proj["id"], "logo.png")

client.set_brand({"display_name": "ACME", "about": "We reply fast"})
client.upload_brand_logo("logo.png")
client.apply_brand()

client.invite_user("bob@acme.com", role="agent")
client.update_account("ACME Ltda")
client.get_me()
```

## Plan, add-ons and invoices

```python
client.get_my_entitlements()
client.upgrade_plan()                       # Pro goes to the cart
client.change_addon("number", 2)            # +2 numbers in the cart
client.get_addon_cart()
client.checkout_addon_cart(save_card=True)  # opens the in-app payment
client.list_my_invoices()
client.get_pricing()
```

## Realtime (SSE)

The API exposes a server-sent-events stream at `GET /stream` for inbound
messages and status updates. It is not wrapped by this SDK — connect with any
SSE client, sending the same `Authorization: Bearer <api_key>` header.

## API keys

```python
client.list_keys()
created = client.create_key("CI key", role="agent")  # role: "admin" | "agent"
print(created["api_key"])  # raw key — shown only once, store it now
client.revoke_key(created["key"]["id"])
```

### Rotating a key without downtime

`rotate_key()` (admin only) mints a **new** key that inherits the old one's name,
role, scopes and project, and keeps the old one working for a grace period — so a
deploy in flight does not break halfway through the swap. Past the deadline the old
key answers `401 key_expired`. `revoke_in_seconds=0` kills it right away; the
default is `86400` (24 h) and the maximum is `2592000` (30 days). The rotated key
carries `expires_at` (when it stops working) and `rotated_to` (the id of its
replacement); partner keys rotate through `PartnerClient.rotate_connection_key`.

```python
rotated = client.rotate_key(created["key"]["id"], revoke_in_seconds=3600)
print(rotated["api_key"])            # raw NEW key — shown only once, store it now
print(rotated["old_key_expires_at"]) # when the old one stops working (None = already dead)
print(rotated["key"]["id"], rotated["previous_key"]["rotated_to"])
```

Errors here are `admin_required` (403), `not_found` (404), `key_already_revoked`
and `key_already_expired` (409).

## Usage

```python
client.get_usage()  # whole period
client.get_usage(from_="2026-06-01T00:00:00Z", to="2026-06-30T23:59:59Z")  # RFC3339
```

## Webhooks

**Manage** your webhook subscriptions:

```python
hook = client.create_webhook(
    "https://yourapp.com/webhooks/bzapper",
    event_types=["message.received", "instance.banned"],  # omit = all events
)
print(hook["secret"])  # signing secret — returned ONCE, store it now
client.list_webhooks()
client.update_webhook(hook["id"], active=False)            # pause
client.update_webhook(hook["id"], secret="regenerate")     # rotate secret
client.trigger_webhook_event("message.received")  # like `stripe trigger`
client.delete_webhook(hook["id"])
```

**Receive and process** deliveries — `bzapper.webhooks` verifies the HMAC
signature, parses the envelope into a typed event, and routes it to your
handlers:

```python
from bzapper.webhooks import Webhooks

hooks = Webhooks(secret="whsec_...")  # the secret from create_webhook

@hooks.on("message.received")
def _(event):
    print(event.sender.name, event.payload.get("body"))

@hooks.on("instance.banned")
def _(event):
    alert(event.instance_id)

# In your HTTP endpoint (framework-agnostic). Pass the RAW body bytes and the
# X-Bzapper-Signature header. Raises SignatureError if the signature is invalid.
hooks.handle(raw_body=request.get_data(), signature=request.headers["X-Bzapper-Signature"])
```

The typed `event` has `id`, `type`, `timestamp`, `instance_id`,
`client_reference`, `group`, `sender`, `mentions`, `payload` and the original
`raw` dict. Use `event.id` for idempotency (the API may retry deliveries).
For lower-level use there's `verify_webhook(secret, body, signature)` and
`construct_webhook_event(secret, body, signature)`.

## bZapper Connect

**bZapper Connect** lets a partner software let ITS customers subscribe to
bZapper Pro and connect WhatsApp without leaving the partner's product. The
partner receives an API key authorized by the customer; the customer stays a
direct bZapper account.

The partner **backend** authenticates with the partner secret (`bz_partner_...`,
sent as `Authorization: Bearer bz_partner_...`). Never send it to a browser.

The flow:

1. Your backend creates a session: `create_connect_session()` → `session_token` (30 min).
2. Your front end opens the embedded component with that token
   (`BzapperConnect.open({ session })`).
3. When the customer finishes (Pro paid + WhatsApp connected), the component emits
   `bzapper:complete` with a one-time `code` (valid 10 min).
4. Your backend exchanges it: `exchange_code(code)` → the customer's `api_key`
   (`bz_live_...`, shown once — store it).
5. Your backend uses the regular `Client(api_key)` for that customer.

### Partner client

```python
from bzapper import PartnerClient

partner = PartnerClient("bz_partner_...")  # base_url / locale / timeout optional, like Client

partner.me()  # who the secret belongs to

session = partner.create_connect_session(
    "customer-42",                        # external_id: YOUR id; same id = same connection
    {
        "name": "Ana Souza",              # name OR company is required
        "email": "ana@boxy.com",          # required
        "phone": "+5511988887777",        # optional, pre-fills the number
        "company": "Boxy Pharma",         # optional, becomes the account/project name
        "country": "BR",                  # optional, sets the currency
    },
    locale="pt-BR",
)
session["session_token"], session["expires_at"], session["connection"]

conn = partner.exchange_code("cc_91ab...")  # -> connection + "api_key" (shown once)

partner.list_connections()                                    # -> {"data": [...]}
partner.list_connections(external_id="customer-42", status="active")
partner.get_connection(conn["id"])
partner.rotate_connection_key(conn["id"])  # new api_key; the previous one stops working
partner.revoke_connection(conn["id"])      # 204; does NOT cancel the customer's plan
```

Connection `status`: `pending_account`, `pending_payment`, `pending_number`,
`active`, `suspended`, `revoked`.

With the customer's key, two error codes are specific to Connect:

| `code`              | HTTP | Meaning |
|---------------------|------|---------|
| `connect_suspended` | 402  | The customer's Pro is unpaid. Resumes **by itself** once paid (`connect.resumed`). |
| `connect_revoked`   | 401  | The connection ended (by the customer, by you, or account deletion). |

### Partner webhooks

Your partner webhook is signed with the **same** HMAC scheme
(`X-Bzapper-Signature: sha256=<hex>` over the raw body), using your partner
webhook secret — so `Webhooks` works unchanged. The envelope is the regular one
plus `connection` (`event.connection`: `id`, `external_id`, `account_id`,
`project_id`, `status`). Besides project events of active connections
(messages, number status…), you receive the lifecycle events
`connect.completed`, `connect.suspended`, `connect.resumed` and
`connect.revoked` (also in `bzapper.webhooks.CONNECT_EVENT_TYPES`).

### Complete backend example (Flask)

```python
import os

from flask import Flask, abort, jsonify, request

from bzapper import BzapperError, Client, PartnerClient
from bzapper.webhooks import SignatureError, Webhooks

app = Flask(__name__)
partner = PartnerClient(os.environ["BZAPPER_PARTNER_SECRET"])
hooks = Webhooks(secret=os.environ["BZAPPER_PARTNER_WEBHOOK_SECRET"])


# 1) Your front end calls this, then opens BzapperConnect with the token.
@app.post("/whatsapp/connect-session")
def connect_session():
    user = current_user()  # your own auth
    s = partner.create_connect_session(
        external_id=str(user.id),
        customer={"name": user.name, "email": user.email, "company": user.company},
        locale="pt-BR",
    )
    return jsonify(session=s["session_token"])


# 2) The component emitted `bzapper:complete` with a code: exchange it on the backend.
@app.post("/whatsapp/connect-complete")
def connect_complete():
    code = request.get_json()["code"]
    try:
        conn = partner.exchange_code(code)
    except BzapperError as err:
        return jsonify(error=err.code), err.status_code
    save_customer_key(conn["external_id"], conn["id"], conn["api_key"])  # store it (encrypted)
    return jsonify(status=conn["status"])


# 3) Use the customer's key with the regular client.
@app.post("/whatsapp/send")
def send():
    user = current_user()
    client = Client(load_customer_key(user.id))
    try:
        res = client.send_text(request.get_json()["to"], request.get_json()["body"])
    except BzapperError as err:
        if err.code == "connect_suspended":   # 402: customer's Pro unpaid
            return jsonify(error="Your WhatsApp plan is unpaid. Update the payment to resume."), 402
        if err.code == "connect_revoked":     # 401: connection ended
            forget_customer_key(user.id)
            return jsonify(error="WhatsApp disconnected. Connect again."), 409
        raise
    return jsonify(res)


# 4) Partner webhook: verify the signature, then react to connect.* events.
@hooks.on("connect.completed")
def _(event):
    mark_whatsapp(event.connection.external_id, "active")

@hooks.on("connect.suspended")
def _(event):
    mark_whatsapp(event.connection.external_id, "suspended")  # show "payment pending"

@hooks.on("connect.resumed")
def _(event):
    mark_whatsapp(event.connection.external_id, "active")

@hooks.on("connect.revoked")
def _(event):
    forget_customer_key(event.connection.external_id)

@hooks.on("message.received")
def _(event):
    route_inbound(event.connection.external_id, event.payload)


@app.post("/webhooks/bzapper")
def bzapper_webhook():
    try:
        event = hooks.handle(
            raw_body=request.get_data(),  # RAW bytes, never re-serialized JSON
            signature=request.headers.get("X-Bzapper-Signature"),
        )
    except SignatureError:
        abort(400)
    # event.id is stable: skip duplicates, the API retries failed deliveries.
    return "", 204
```

### Connected apps (customer side)

With the customer's own key, list and disconnect partner apps using the
account's WhatsApp:

```python
client.list_connected_apps()            # -> {"data": [{"id", "partner_name", "status", ...}]}
client.revoke_connected_app("conn_id")  # admin; the partner's key stops working immediately
```

## Errors, retries and idempotency

Non-2xx responses raise `BzapperError` — or a typed subclass — with a **stable
`code`**, a localized `message` (never parse it), `status_code` (also `status`),
`request_id` (quote it to support), `retry_after` (429), `required_scope` (403)
and the decoded `body`. **Branch on `code`.**

| Class | When |
|---|---|
| `AuthenticationError` | 401 (also `connect_revoked`) |
| `PermissionDeniedError` | 403 — see `required_scope` |
| `NotFoundError` | 404 |
| `ConflictError` | 409 |
| `ValidationError` | 400 / 422 |
| `RateLimitError` | 429 — see `retry_after` |
| `ServerError` | 5xx |
| `NetworkError` | connection failure/timeout (`status_code == 0`, `code == "NETWORK_ERROR"`) |
| `BzapperError` | anything else (e.g. 402 `connect_suspended`), and `INVALID_RESPONSE` when a 2xx is not JSON |

An empty key or an empty/`.`/`..` path parameter raises `ValueError` before any
request.

```python
from bzapper import BzapperError, RateLimitError

try:
    client.send_text("+5511999999999", "Hi", idempotency_key="order-42")
except RateLimitError as err:
    print("slow down for", err.retry_after, "s")
except BzapperError as err:
    if err.code == "not_connected":
        ...  # reconnect flow
    else:
        print(err.code, err.status_code, err.message, err.request_id)
```

**Automatic retries.** Network errors/timeouts, `429`, `502`, `503` and `504` are
retried up to `max_retries` times (default 2), honoring `Retry-After` (capped at
60 s) or exponential backoff (`0.5 × 2^n` s, max 8 s, + jitter). A `500` or any
`4xx` is returned at once. Every attempt of one call carries the **same**
`X-Request-Id` and `Idempotency-Key`, so a retried write never runs twice (the API
replays the first response with `Idempotent-Replayed: true`, for 24 h).

## Example

A runnable script is in [`examples/quickstart.py`](examples/quickstart.py).

## License

MIT © Berni Software
