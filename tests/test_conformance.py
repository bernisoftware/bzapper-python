"""Conformance: runs EVERY case of ``tests/fixtures/cases.json`` (BRIEF §7).

For each case a local HTTP server checks every exchange — method, path (raw, segment by
segment), query, JSON body (or multipart), ``Authorization``, ``X-Bzapper-Client``,
``X-Request-Id``, ``Idempotency-Key`` — and, across retries of the same call, that the
ids repeat. Then the returned value (or the raised error) is compared with ``expect``.

A new endpoint without an SDK method breaks this suite: every op in ``ops`` must be in
``OPS`` below (a missing op FAILS, it is never skipped).
"""

from __future__ import annotations

import base64
import copy
import json
import re
import unittest
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote

import bzapper
from bzapper import Client, PartnerClient, Webhooks, errors
from bzapper.webhooks import SignatureError, verify

from _support import MONOREPO_CASES, VENDORED_CASES, FakeServer, load_cases

CASES = load_cases()
API_KEY = CASES["api_key"]
MAX_RETRIES = CASES.get("max_retries", 2)
EXCLUDED = set(CASES.get("sdk_excluded_ops", []))
CLIENT_RE = re.compile(r"^bzapper-python/" + re.escape(bzapper.__version__) + r"$")
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
WRITES = {"POST", "PUT", "PATCH", "DELETE"}
SENT = "$sent"  # in expect.error.request_id: "the X-Request-Id the SDK sent"

Args = Dict[str, Any]
Call = Callable[[Client, PartnerClient, Args, Dict[str, Any]], Any]


def _flat(a: Args) -> Dict[str, Any]:
    """Query + body as keyword arguments (their names match the SDK parameters)."""
    kwargs: Dict[str, Any] = dict(a.get("query") or {})
    kwargs.update(a.get("body") or {})
    return kwargs


def client(name: str) -> Call:
    """Generic call: path params positionally (in path order), query + body as kwargs."""
    def call(c: Client, p: PartnerClient, a: Args, opts: Dict[str, Any]) -> Any:
        return getattr(c, name)(*a["path"].values(), **_flat(a), **opts)
    return call


def partner(name: str) -> Call:
    def call(c: Client, p: PartnerClient, a: Args, opts: Dict[str, Any]) -> Any:
        return getattr(p, name)(*a["path"].values(), **_flat(a), **opts)
    return call


def usage(name: str) -> Call:
    """``from`` is a Python keyword: the SDK names it ``from_``."""
    def call(c: Client, p: PartnerClient, a: Args, opts: Dict[str, Any]) -> Any:
        q = dict(a["query"])
        if "from" in q:
            q["from_"] = q.pop("from")
        return getattr(c, name)(**q, **opts)
    return call


def profile(name: str) -> Call:
    """BrandProfile goes as ONE dict argument (``set_brand(profile)``)."""
    def call(c: Client, p: PartnerClient, a: Args, opts: Dict[str, Any]) -> Any:
        return getattr(c, name)(*a["path"].values(), a["body"], **opts)
    return call


def upload(name: str) -> Call:
    """Multipart: ``file`` arrives as base64 + filename + content type."""
    def call(c: Client, p: PartnerClient, a: Args, opts: Dict[str, Any]) -> Any:
        f = a["body"]["file"]
        content = base64.b64decode(f["content_base64"])
        return getattr(c, name)(
            *a["path"].values(), content, f.get("filename"), f.get("content_type"), **opts
        )
    return call


# op (neutral operationId) → SDK call. Existing methods keep their historical names
# (list_keys, group_invite, webhook_deliveries, PartnerClient.me …).
OPS: Dict[str, Call] = {
    # me / account / keys / health
    "getMe": client("get_me"),
    "updateProfile": client("update_profile"),
    "updateAccount": client("update_account"),
    "getHealth": client("get_health"),
    "listMyKeys": client("list_keys"),
    "createMyKey": client("create_key"),
    "revokeMyKey": client("revoke_key"),
    # brand
    "getBrand": client("get_brand"),
    "setBrand": profile("set_brand"),
    "applyBrand": client("apply_brand"),
    "uploadBrandLogo": upload("upload_brand_logo"),
    # contacts (CRM)
    "listContacts": client("list_contacts"),
    "createContact": client("create_contact"),
    "getContact": client("get_contact"),
    "updateContact": client("update_contact"),
    "deleteContact": client("delete_contact"),
    "getContactHistory": client("get_contact_history"),
    "addContactNote": client("add_contact_note"),
    "mutateContactTags": client("mutate_contact_tags"),
    "mutateContactGroups": client("mutate_contact_groups"),
    "optOutContact": client("opt_out_contact"),
    "suppressContact": client("suppress_contact"),
    "optInContact": client("opt_in_contact"),
    "contactsCheck": client("contacts_check"),
    "listTags": client("list_tags"),
    "createTag": client("create_tag"),
    "deleteTag": client("delete_tag"),
    "listContactGroups": client("list_contact_groups"),
    "createContactGroup": client("create_contact_group"),
    "deleteContactGroup": client("delete_contact_group"),
    "listSuppressions": client("list_suppressions"),
    "createSuppression": client("create_suppression"),
    "deleteSuppression": client("delete_suppression"),
    # projects / users / usage
    "listProjects": client("list_projects"),
    "createProject": client("create_project"),
    "getProjectsHealth": client("get_projects_health"),
    "updateProject": client("update_project"),
    "deleteProject": client("delete_project"),
    "getProjectBrand": client("get_project_brand"),
    "setProjectBrand": profile("set_project_brand"),
    "uploadProjectLogo": upload("upload_project_logo"),
    "listUsers": client("list_users"),
    "inviteUser": client("invite_user"),
    "updateUserRole": client("update_user_role"),
    "removeUser": client("remove_user"),
    "getAccountUsage": usage("get_account_usage"),
    "getUsage": usage("get_usage"),
    # billing
    "getMyEntitlements": client("get_my_entitlements"),
    "upgradePlan": client("upgrade_plan"),
    "cancelPlan": client("cancel_plan"),
    "uncancelPlan": client("uncancel_plan"),
    "getMySubscription": client("get_my_subscription"),
    "changeAddon": client("change_addon"),
    "getAddonCart": client("get_addon_cart"),
    "clearAddonCart": client("clear_addon_cart"),
    "checkoutAddonCart": client("checkout_addon_cart"),
    "listMyInvoices": client("list_my_invoices"),
    "payInvoice": client("pay_invoice"),
    "getBillingConfig": client("get_billing_config"),
    "getPricing": client("get_pricing"),
    # messages
    "sendText": client("send_text"),
    "sendImage": client("send_image"),
    "sendVideo": client("send_video"),
    "sendDocument": client("send_document"),
    "sendAudio": client("send_audio"),
    "sendSticker": client("send_sticker"),
    "sendLocation": client("send_location"),
    "sendContact": client("send_contact"),
    "sendPoll": client("send_poll"),
    "sendReaction": client("send_reaction"),
    "sendButtons": client("send_buttons"),
    "sendList": client("send_list"),
    "sendOTP": client("send_otp"),
    "listScheduled": client("list_scheduled"),
    "cancelScheduled": client("cancel_scheduled"),
    "editMessage": client("edit_message"),
    "revokeMessage": client("revoke_message"),
    "forwardMessage": client("forward_message"),
    "markRead": client("mark_read"),
    "presenceChat": client("presence_chat"),
    # chats / labels / block list / calls / conversations
    "archiveChat": client("archive_chat"),
    "pinChat": client("pin_chat"),
    "markChat": client("mark_chat"),
    "muteChat": client("mute_chat"),
    "applyChatLabel": client("apply_chat_label"),
    "listLabels": client("list_labels"),
    "createLabel": client("create_label"),
    "deleteLabel": client("delete_label"),
    "blockContact": client("block_contact"),
    "unblockContact": client("unblock_contact"),
    "getBlocklist": client("get_blocklist"),
    "rejectCall": client("reject_call"),
    "offerCall": client("offer_call"),
    "listConversations": client("list_conversations"),
    "conversationHistory": client("conversation_history"),
    # instances (numbers)
    "listInstances": client("list_instances"),
    "createInstance": client("create_instance"),
    "getInstance": client("get_instance"),
    "deleteInstance": client("delete_instance"),
    "connectInstance": client("connect_instance"),
    "disconnectInstance": client("disconnect_instance"),
    "logoutInstance": client("logout_instance"),
    "clearInstanceSession": client("clear_instance_session"),
    "archiveInstance": client("archive_instance"),
    "unarchiveInstance": client("unarchive_instance"),
    "setInstanceProxy": client("set_instance_proxy"),
    "setInboundFilters": client("set_inbound_filters"),
    "setProfile": client("set_profile"),
    "setPrivacy": client("set_privacy"),
    "getOfficialAccount": client("get_official_account"),
    "connectOfficialAccount": client("connect_official_account"),
    "disconnectOfficialAccount": client("disconnect_official_account"),
    # groups
    "listGroups": client("list_groups"),
    "createGroup": client("create_group"),
    "joinGroup": client("join_group"),
    "previewGroupInvite": client("preview_group_invite"),
    "getGroup": client("get_group"),
    "updateGroup": client("update_group"),
    "updateGroupParticipants": client("update_group_participants"),
    "groupInviteLink": client("group_invite"),
    "leaveGroup": client("leave_group"),
    "listJoinRequests": client("list_join_requests"),
    "updateJoinRequests": client("update_join_requests"),
    # campaigns
    "listCampaigns": client("list_campaigns"),
    "createCampaign": client("create_campaign"),
    "estimateCampaign": client("estimate_campaign"),
    "getCampaignEligibility": client("get_campaign_eligibility"),
    "uploadCampaignMedia": upload("upload_campaign_media"),
    "getCampaign": client("get_campaign"),
    "updateCampaign": client("update_campaign"),
    "listCampaignRecipients": client("list_campaign_recipients"),
    "addCampaignRecipients": client("add_campaign_recipients"),
    "startCampaign": client("start_campaign"),
    "pauseCampaign": client("pause_campaign"),
    "resumeCampaign": client("resume_campaign"),
    "cancelCampaign": client("cancel_campaign"),
    "dryRunCampaign": client("dry_run_campaign"),
    # pools
    "listPools": client("list_pools"),
    "createPool": client("create_pool"),
    "getPool": client("get_pool"),
    "addPoolNumber": client("add_pool_number"),
    # advisories / webhooks
    "listAdvisories": client("list_advisories"),
    "markAdvisoryRead": client("mark_advisory_read"),
    "listWebhooks": client("list_webhooks"),
    "createWebhook": client("create_webhook"),
    "updateWebhook": client("update_webhook"),
    "deleteWebhook": client("delete_webhook"),
    "testWebhook": client("test_webhook"),
    "listWebhookDeliveries": client("webhook_deliveries"),
    "triggerWebhookEvent": client("trigger_webhook_event"),
    # bZapper Connect — customer side
    "listConnectedApps": client("list_connected_apps"),
    "revokeConnectedApp": client("revoke_connected_app"),
    # bZapper Connect — partner client
    "getPartnerMe": partner("me"),
    "createConnectSession": partner("create_connect_session"),
    "exchangeConnectCode": partner("exchange_code"),
    "listPartnerConnections": partner("list_connections"),
    "getPartnerConnection": partner("get_connection"),
    "revokePartnerConnection": partner("revoke_connection"),
    "rotatePartnerConnectionKey": partner("rotate_connection_key"),
}

ERROR_TYPES = {
    "authentication": errors.AuthenticationError,
    "permission_denied": errors.PermissionDeniedError,
    "not_found": errors.NotFoundError,
    "conflict": errors.ConflictError,
    "validation": errors.ValidationError,
    "rate_limit": errors.RateLimitError,
    "server": errors.ServerError,
    "network": errors.NetworkError,
    "api": errors.BzapperError,
}


def canonical(value: Any) -> str:
    """Canonical form: tells ``true`` from ``1`` (in Python ``True == 1``)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def segments(raw_path: str) -> List[str]:
    return [unquote(s) for s in raw_path.split("/")]


class ConformanceTest(unittest.TestCase):
    server: FakeServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FakeServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def assertJSONEqual(self, actual: Any, expected: Any, what: str) -> None:
        self.assertEqual(actual, expected, what)
        self.assertEqual(canonical(actual), canonical(expected), f"{what} (JSON types)")

    def run_case(self, case: Dict[str, Any]) -> None:
        op = case["op"]
        self.assertIn(op, OPS, f"op {op!r} has no SDK method (and is not in sdk_excluded_ops)")
        exchanges = case["exchanges"]
        self.server.reset([ex["response"] for ex in exchanges])

        c = Client(API_KEY, base_url=self.server.base_url, max_retries=MAX_RETRIES)
        p = PartnerClient(API_KEY, base_url=self.server.base_url, max_retries=MAX_RETRIES)
        sleeps: List[float] = []
        c._sleep = sleeps.append  # retry waits disabled: the suite never sleeps
        p._http._sleep = sleeps.append

        opts = dict(case.get("options") or {})
        result: Any = None
        error: Optional[BaseException] = None
        try:
            result = OPS[op](c, p, copy.deepcopy(case["args"]), opts)
        except (errors.BzapperError, ValueError) as exc:
            error = exc

        self.check_exchanges(case, exchanges, self.server.requests)
        self.assertEqual(len(sleeps), max(0, len(exchanges) - 1), "one wait per retry")

        expect = case["expect"]
        if "error" in expect:
            want = expect["error"]
            self.assertIsNotNone(error, f"expected error {want['type']}, got {result!r}")
            if want["type"] == "argument":
                self.assertIsInstance(error, ValueError)
                self.assertNotIsInstance(error, errors.BzapperError)
                return
            assert isinstance(error, errors.BzapperError), repr(error)
            self.assertIs(type(error), ERROR_TYPES[want["type"]],
                          f"error class: {type(error).__name__}")
            self.assertEqual(error.code, want["code"])
            self.assertEqual(error.status_code, want["status"])
            self.assertEqual(error.status, want["status"])
            if "request_id" in want:
                expected_id = want["request_id"]
                if expected_id == SENT:
                    expected_id = self.server.requests[-1]["headers"].get("x-request-id")
                    self.assertTrue(expected_id)
                self.assertEqual(error.request_id, expected_id)
            if "retry_after" in want:
                self.assertEqual(error.retry_after, want["retry_after"])
            if "required_scope" in want:
                self.assertEqual(error.required_scope, want["required_scope"])
        else:
            self.assertIsNone(error, f"unexpected error: {error!r}")
            wanted = expect["result"]
            if (
                isinstance(wanted, dict) and "data" in wanted
                and isinstance(result, list) and canonical(result) == canonical(wanted["data"])
            ):
                return  # SDK that unwraps {data:[...]} (BRIEF §4) — not this one, but allowed
            self.assertJSONEqual(result, wanted, "result")

    def check_exchanges(self, case: Dict[str, Any], exchanges: list, requests: list) -> None:
        self.assertEqual(
            len(requests),
            len(exchanges),
            "request count: " + ", ".join(f"{r['method']} {r['raw_path']}" for r in requests),
        )
        previous = None
        for index, (exchange, got) in enumerate(zip(exchanges, requests)):
            want = exchange["request"]
            where = f"exchange {index}"
            headers = got["headers"]

            self.assertEqual(got["method"], want["method"], f"{where}: method")
            self.assertNotIn(" ", got["raw_path"], f"{where}: raw path has a space")
            self.assertEqual(segments(got["raw_path"]), segments(want["path"]), f"{where}: path")
            self.assertEqual(
                sorted(got["query"]),
                sorted(want["query"].items()),
                f"{where}: query",
            )
            if case.get("multipart"):
                self.assertTrue(
                    headers.get("content-type", "").startswith("multipart/form-data"),
                    f"{where}: Content-Type {headers.get('content-type')!r}",
                )
                filename = case["args"]["body"]["file"]["filename"]
                self.assertIn(filename.encode("utf-8"), got["body"], f"{where}: filename")
            elif want["body"] is None:
                self.assertEqual(got["body"], b"", f"{where}: must not have a body")
                self.assertNotIn("content-type", headers, f"{where}: Content-Type without body")
            else:
                self.assertEqual(headers.get("content-type"), "application/json")
                self.assertJSONEqual(json.loads(got["body"].decode("utf-8")), want["body"],
                                     f"{where}: body")

            self.assertEqual(headers.get("authorization"), f"Bearer {API_KEY}")
            self.assertEqual(headers.get("accept"), "application/json")
            self.assertRegex(headers.get("x-bzapper-client", ""), CLIENT_RE)
            self.assertEqual(headers.get("user-agent"), headers.get("x-bzapper-client"))
            self.assertRegex(headers.get("x-request-id", ""), REQUEST_ID_RE)
            if want["method"] in WRITES:
                self.assertTrue(headers.get("idempotency-key"), f"{where}: Idempotency-Key")
            else:
                self.assertNotIn("idempotency-key", headers, f"{where}: Idempotency-Key on GET")
            for key, value in (want.get("headers") or {}).items():
                self.assertEqual(headers.get(key.lower()), value, f"{where}: header {key}")

            self.assertIn("retry", exchange, f"{where}: exchange without 'retry'")
            if index == 0:
                self.assertFalse(exchange["retry"], "the 1st exchange cannot be a retry")
            elif exchange["retry"]:
                # retry of the SAME logical call: ids repeat
                self.assertEqual(headers.get("x-request-id"), previous.get("x-request-id"),
                                 f"{where}: X-Request-Id of the retry")
                self.assertEqual(headers.get("idempotency-key"), previous.get("idempotency-key"),
                                 f"{where}: Idempotency-Key of the retry")
            else:
                # a new logical call: new ids
                self.assertNotEqual(headers.get("x-request-id"), previous.get("x-request-id"),
                                    f"{where}: new call needs a new X-Request-Id")
                if "idempotency-key" in headers:
                    self.assertNotEqual(headers["idempotency-key"],
                                        previous.get("idempotency-key"))
            previous = headers


def _make_test(case: Dict[str, Any]) -> Callable[[ConformanceTest], None]:
    def test(self: ConformanceTest) -> None:
        self.run_case(case)

    test.__doc__ = f"case {case['id']}"
    return test


for _case in CASES["cases"]:
    # "." is spelled out: "argument/path_." and "argument/path_.." must not collide.
    _name = "test_case_" + re.sub(r"\W+", "_", _case["id"].replace(".", "dot")).strip("_")
    assert not hasattr(ConformanceTest, _name), f"duplicate case id: {_case['id']}"
    setattr(ConformanceTest, _name, _make_test(_case))


class CoverageTest(unittest.TestCase):
    def test_every_op_has_a_method(self) -> None:
        """Every op in ``ops`` must be mapped — a missing one FAILS (never skipped)."""
        self.assertEqual(len(CASES["ops"]), len(set(CASES["ops"])))
        missing = sorted(set(CASES["ops"]) - set(OPS))
        self.assertEqual(missing, [], "ops without an SDK method — implement them")

    def test_every_case_op_is_listed(self) -> None:
        self.assertEqual(sorted({c["op"] for c in CASES["cases"]} - set(CASES["ops"])), [])

    def test_table_points_to_real_methods(self) -> None:
        c = Client(API_KEY)
        p = PartnerClient(API_KEY)
        for op, call in OPS.items():
            name = call.__closure__[0].cell_contents  # type: ignore[index]
            target = p if call.__qualname__.startswith("partner") else c
            with self.subTest(op=op):
                self.assertTrue(callable(getattr(target, name, None)), f"{op} → {name}")

    def test_excluded_are_not_in_the_table(self) -> None:
        self.assertEqual(sorted(EXCLUDED & set(OPS)), [])

    def test_no_stale_entries(self) -> None:
        self.assertEqual(sorted(set(OPS) - set(CASES["ops"])), [])

    @unittest.skipUnless(MONOREPO_CASES.is_file(), "the cases source only exists in the monorepo")
    def test_vendored_copy_is_current(self) -> None:
        self.assertEqual(
            VENDORED_CASES.read_bytes(),
            MONOREPO_CASES.read_bytes(),
            "tests/fixtures/cases.json is stale — run "
            "`python3 clients/conformance/generate.py` (it writes the copy; never edit by hand)",
        )


class SignatureVectorsTest(unittest.TestCase):
    def test_vectors(self) -> None:
        self.assertTrue(CASES["signatures"])
        for vector in CASES["signatures"]:
            with self.subTest(vector=vector.get("id")):
                for body in (vector["body"], vector["body"].encode("utf-8")):
                    self.assertIs(
                        verify(vector["secret"], body, vector["signature"]), vector["valid"]
                    )
                    self.assertIs(
                        bzapper.verify_webhook(vector["secret"], body, vector["signature"]),
                        vector["valid"],
                    )
                hooks = Webhooks(vector["secret"])
                if vector["valid"]:
                    try:
                        json.loads(vector["body"])
                    except ValueError:
                        continue
                    hooks.construct_event(vector["body"], vector["signature"])
                else:
                    with self.assertRaises(SignatureError):
                        hooks.construct_event(vector["body"], vector["signature"])


if __name__ == "__main__":
    unittest.main()
