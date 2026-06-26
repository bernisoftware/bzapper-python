"""bZapper Python SDK — quickstart.

Run:
    pip install bzapper
    BZAPPER_API_KEY=bz_live_... python examples/quickstart.py
"""

import os

from bzapper import BzapperError, Client


def main() -> None:
    client = Client(
        base_url=os.environ.get("BZAPPER_BASE_URL", "http://localhost:8080"),
        api_key=os.environ.get("BZAPPER_API_KEY", "bz_live_..."),
        locale="pt-BR",
    )

    to = os.environ.get("BZAPPER_TO", "+5511999999999")

    try:
        # Text
        res = client.send_text(to, "Hello from the bZapper Python SDK!")
        print("queued:", res)

        # Image (URL)
        client.send_image(to, {"url": "https://picsum.photos/600", "caption": "Hi"})

        # Video
        client.send_video(to, {"url": "https://example.com/clip.mp4"})

        # Document
        client.send_document(to, {"url": "https://example.com/invoice.pdf",
                                  "filename": "invoice.pdf"})

        # Audio as a voice note (ptt)
        client.send_audio(to, {"url": "https://example.com/note.ogg", "ptt": True})

        # Sticker
        client.send_sticker(to, {"url": "https://example.com/sticker.webp"})

        # Location
        client.send_location(to, -23.5613, -46.6565, name="Av. Paulista")

        # Contact
        client.send_contact(to, contact_name="Berni Software")

        # Poll
        client.send_poll(to, "Pizza or sushi?", ["Pizza", "Sushi"])

        # Reaction (needs a wa_message_id to react to)
        client.send_reaction(to, quoted_message_id="ABCD1234", emoji="👍")

        # Buttons (falls back to a numbered text menu)
        client.send_buttons(
            to,
            "Choose an option:",
            [{"id": "a", "title": "Option A"}, {"id": "b", "title": "Option B"}],
            footer="Powered by bZapper",
        )

        # List (falls back to a numbered text menu)
        client.send_list(
            to,
            "Pick from the menu:",
            [
                {
                    "title": "Drinks",
                    "rows": [
                        {"id": "1", "title": "Coffee", "description": "Hot"},
                        {"id": "2", "title": "Tea"},
                    ],
                }
            ],
            button_text="Open menu",
        )

        # Instances
        print("instances:", client.list_instances())

        # --- Advanced: presence, conversations and groups ---
        instance_id = os.environ.get("BZAPPER_INSTANCE_ID")
        if instance_id:
            # Presence works in groups too — pass a group JID as `to`.
            client.presence_chat(instance_id, to, "typing")

            # Conversations & history
            print("conversations:", client.list_conversations(instance_id))

            # Groups
            print("groups:", client.list_groups(instance_id))
            group = client.create_group(instance_id, "bZapper demo", [to])
            client.presence_chat(instance_id, group["jid"], "typing")  # group presence
            print("invite:", client.group_invite(group["jid"], instance_id))

            # Which numbers are on WhatsApp?
            print("check:", client.contacts_check(instance_id, [to]))

        # Usage
        print("usage:", client.get_usage())

    except BzapperError as err:
        # Always branch on the stable code, never on the translated message.
        print(f"error code={err.code} status={err.status_code}: {err.message}")


if __name__ == "__main__":
    main()
