# bzapper

Official **Python SDK** for the [bZapper](https://bzapper.com.br) WhatsApp gateway API — a multi-tenant WhatsApp gateway: connect numbers, send and receive messages, rotate numbers (anti-ban) and track usage.

Zero runtime dependencies (pure standard library). Python 3.9+.

## Install

```bash
pip install bzapper
```

## Hello world

```python
from bzapper import Client

client = Client("http://localhost:8080", "bz_live_...")
client.send_text("+5511999999999", "Hello from bZapper!")
```

## Client configuration

```python
from bzapper import Client

client = Client(
    base_url="https://api.bzapper.com.br",  # http://localhost:8080 in dev
    api_key="bz_live_...",                    # tenant API key
    locale="pt-BR",                            # optional, sets Accept-Language
    timeout=30,                                # optional, seconds
)
```

Every request sends `Authorization: Bearer <api_key>`, `Content-Type: application/json` and, when `locale` is set, `Accept-Language: <locale>`.

## Messages

Every message method accepts the common **SendBase** options as keyword
arguments: `instance_id`, `pool_id`, `quoted_message_id`, `client_reference`
and `mentions`. Each returns the queued-message object
(`message_id`, `status`, optional `client_reference`).

`to` is a phone in E.164 (`+5511999999999`) or a JID.

```python
# Text
client.send_text("+5511999999999", "Hello!")

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

# Update the WhatsApp profile (display name / about / picture)
client.set_profile(inst["id"], display_name="Support", status_message="We reply fast")
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

# Groups
client.list_groups(inst_id)
group = client.create_group(inst_id, "My group", ["+5511999999999", "+5511888888888"])
client.get_group(group["jid"], inst_id)
client.update_group_participants(
    group["jid"], inst_id, "add", ["+5511777777777"]  # add|remove|promote|demote
)
client.group_invite(group["jid"], inst_id)          # -> invite link/code
client.join_group(inst_id, "Cabc123InviteCode")     # join via invite code
client.leave_group(group["jid"], inst_id)

# Contacts — which numbers are on WhatsApp?
client.contacts_check(inst_id, ["+5511999999999", "+5511888888888"])
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

## Error handling

Non-2xx responses raise `BzapperError` with a **stable `code`**, a localized
`message` and the `status_code`. Always branch on `code` — never parse the
human-readable `message`.

```python
from bzapper import BzapperError

try:
    client.send_text("+5511999999999", "Hi")
except BzapperError as err:
    if err.code == "instance_not_connected":
        # reconnect flow...
        ...
    elif err.code == "rate_limited":
        # back off...
        ...
    else:
        print(err.code, err.status_code, err.message)
```

## Example

A runnable script is in [`examples/quickstart.py`](examples/quickstart.py).

## License

MIT © Berni Software
