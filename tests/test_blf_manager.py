import time
import unittest

from blf import BLFButton, BLFManager

NS = 'xmlns="urn:freedeswitch:blf"'


def body(inner):
    return f'<event name="dialog" {NS}>{inner}</event>'


def instance(name, rpid, **extra):
    params = [f'<param name="rpid" value="{rpid}"/>']
    for key, value in extra.items():
        params.append(f'<param name="{key}" value="{value}"/>')
    return f'<instance name="{name}"><params>{"".join(params)}</params></instance>'


class RecordingLog:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    def __contains__(self, fragment):
        return any(fragment in line for line in self.lines)


class FakeDialog:
    def __init__(self, target_uri):
        self.target_uri = target_uri
        self.call_id = f"call-{target_uri}"
        self.subscribes = []
        self.refreshes = []
        self.unsubscribes = []
        self.to_tag = None

    def send_subscribe(self, event, accept, expires, event_id):
        self.subscribes.append((event, accept, expires, event_id))

    def send_refresh(self, expires):
        self.refreshes.append((expires, self.to_tag))

    def send_unsubscribe(self):
        self.unsubscribes.append(self.to_tag)


class FakeClient:
    def __init__(self):
        self.dialogs = []
        self.sent = []

    def create_dialog(self, target_uri):
        dialog = FakeDialog(target_uri)
        self.dialogs.append(dialog)
        return dialog

    def send(self, data):
        self.sent.append(data)
        return len(data)


class FakeMessage:
    def __init__(self, method, body="", headers=None, status_code=None, reason=None):
        self.method = method
        self.body = body
        self.headers = headers or {}
        self.status_code = status_code
        self.reason = reason


class Response:
    def __init__(self, code, reason, headers=None):
        self.method = None
        self.status_code = code
        self.reason = reason
        self.body = ""
        self.headers = headers or {}


def notify(instances_xml):
    return FakeMessage("NOTIFY", body=instances_xml,
                       headers={"Content-Type": "application/blf+xml"})


def make_manager(refresh=1800, expires=3600, log=None):
    log = log if log is not None else RecordingLog()
    buttons = [BLFButton("101", "101", "default", "Operator 1"),
               BLFButton("102", "102", "default", "Manager")]
    client = FakeClient()
    return client, BLFManager(client, buttons, expires=expires, refresh=refresh,
                              log=log), log


class SubscribeTest(unittest.TestCase):
    def test_subscribe_all_opens_one_dialog_per_button(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.assertEqual(len(client.dialogs), 2)
        self.assertEqual([d.target_uri for d in client.dialogs],
                         ["sip:101@default", "sip:102@default"])

    def test_subscribe_carries_event_accept_expires_and_event_id(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.assertEqual(client.dialogs[0].subscribes,
                         [("dialog", "application/blf+xml", 3600,
                           "blf-101@default")])

    def test_logs_each_subscription(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.assertIn("SUBSCRIBE 101", log)
        self.assertIn("sip:101@default", log)
        self.assertIn("SUBSCRIBE 102", log)

    def accepted(self, manager, name="101"):
        call_id = manager._dialogs[name].call_id
        manager.handle_response(Response(
            200, "OK", headers={"Call-ID": call_id,
                               "To": "<sip:101@default>;tag=serverTAG"}))

    def test_resubscribing_reuses_the_same_dialog(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.accepted(manager)
        manager.subscribe_all()
        self.assertEqual(len(client.dialogs), 2)
        self.assertEqual(len(client.dialogs[0].subscribes), 1)
        self.assertEqual(len(client.dialogs[0].refreshes), 1)

    def test_resubscribe_before_the_answer_resends_rather_than_refreshes(self):
        # Until the 2xx arrives there is no To tag, so a re-SUBSCRIBE is a
        # retransmission of the same request, not a new dialog to extend.
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.subscribe_all()
        self.assertEqual(len(client.dialogs), 2)
        self.assertEqual(len(client.dialogs[0].subscribes), 2)
        self.assertEqual(client.dialogs[0].refreshes, [])

    def test_refresh_reuses_the_original_expires(self):
        # A re-SUBSCRIBE re-sends the original Expires. blf.refresh only
        # decides WHEN the client re-subscribes; it is never the Expires value.
        client, manager, log = make_manager(refresh=1800, expires=3600)
        manager.subscribe_all()
        self.accepted(manager)
        manager.subscribe_all()
        self.assertEqual(client.dialogs[0].refreshes, [(3600, "serverTAG")])

    def test_refresh_due_refreshes_only_expired_buttons(self):
        client, manager, log = make_manager(refresh=1800, expires=3600)
        manager.subscribe_all()
        self.accepted(manager)
        manager._next_refresh["101"] = time.time() - 1
        manager.refresh_due()
        self.assertEqual(len(client.dialogs[0].refreshes), 1)
        self.assertEqual(client.dialogs[1].refreshes, [])

    def test_refresh_due_does_nothing_before_it_is_time(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.refresh_due()
        self.assertEqual(client.dialogs[0].refreshes, [])
        self.assertEqual(client.dialogs[1].refreshes, [])

    def test_force_refresh_hits_every_button(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.force_refresh()
        self.assertEqual(len(client.dialogs[0].refreshes), 1)
        self.assertEqual(len(client.dialogs[1].refreshes), 1)

    def test_force_refresh_before_subscribe_is_harmless(self):
        client, manager, log = make_manager()
        manager.force_refresh()
        self.assertEqual(client.dialogs, [])

    def test_unsubscribe_all_sends_for_every_button(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.unsubscribe_all()
        self.assertEqual(len(client.dialogs[0].unsubscribes), 1)
        self.assertEqual(len(client.dialogs[1].unsubscribes), 1)

    def test_unsubscribe_before_subscribe_is_harmless(self):
        client, manager, log = make_manager()
        manager.unsubscribe_all()
        # Nothing to unsubscribe, so no dialog may be conjured up and nothing
        # may be logged.
        self.assertEqual(client.dialogs, [])
        self.assertEqual([line for line in log.lines if "UNSUBSCRIBE" in line], [])


class HandleNotifyTest(unittest.TestCase):
    def test_updates_matching_button_state(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        changes = manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        self.assertEqual([b.name for b in changes], ["101"])
        self.assertEqual(manager.get("101").state, "ringing")

    def test_matches_by_number_and_domain(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(notify(body(instance("102@default", "busy"))))
        self.assertEqual(manager.get("102").state, "busy")

    def test_matches_a_button_whose_number_differs(self):
        button = BLFButton("operator", "101", "default")
        client = FakeClient()
        manager = BLFManager(client, [button], log=RecordingLog())
        manager.subscribe_all()
        manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        self.assertEqual(button.state, "ringing")

    def test_updates_several_buttons_from_one_notify(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        raw = body(instance("101@default", "ringing")
                   + instance("102@default", "held"))
        changes = manager.handle_notify(notify(raw))
        self.assertEqual(sorted(b.name for b in changes), ["101", "102"])
        self.assertEqual(manager.get("101").state, "ringing")
        self.assertEqual(manager.get("102").state, "held")

    def test_caller_number_is_recorded(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        raw = body(instance("101@default", "ringing",
                            caller_id_number="5551234"))
        manager.handle_notify(notify(raw))
        self.assertEqual(manager.get("101").caller_number, "5551234")

    def test_logs_the_transition(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        self.assertIn("101 (Operator 1): unknown -> ringing", log)

    def test_no_log_when_nothing_changed(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        raw = body(instance("101@default", "ringing"))
        manager.handle_notify(notify(raw))
        before = len(log.lines)
        manager.handle_notify(notify(raw))
        self.assertEqual(len(log.lines), before)

    def test_unknown_instance_is_ignored(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        changes = manager.handle_notify(notify(body(instance("999@default", "ringing"))))
        self.assertEqual(changes, [])
        self.assertEqual(manager.get("101").state, "unknown")

    def test_non_notify_is_ignored(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.assertEqual(manager.handle_notify(FakeMessage("INVITE")), [])

    def test_wrong_content_type_is_ignored(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        msg = FakeMessage("NOTIFY", body=body(instance("101@default", "ringing")),
                          headers={"Content-Type": "application/simple-message-summary"})
        self.assertEqual(manager.handle_notify(msg), [])

    def test_malformed_body_is_ignored(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.assertEqual(manager.handle_notify(notify("<event><instance>")), [])

    def test_malformed_body_is_logged_once(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(notify("<event><instance></event>"))
        manager.handle_notify(notify("<event><instance></event>"))
        hits = [line for line in log.lines if "could not be parsed" in line]
        self.assertEqual(len(hits), 1)

    def test_parse_logging_resumes_after_a_good_notify(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(notify("<event><instance></event>"))
        manager.handle_notify(notify(body(instance("101@default", "ringing"))))
        manager.handle_notify(notify("<event><instance></event>"))
        hits = [line for line in log.lines if "could not be parsed" in line]
        self.assertEqual(len(hits), 2)

    def test_snapshot_preserves_order(self):
        _, manager, log = make_manager()
        self.assertEqual([b.name for b in manager.snapshot()], ["101", "102"])

    def test_get_unknown_name_returns_none(self):
        _, manager, log = make_manager()
        self.assertIsNone(manager.get("777"))


class SubscribeResponseTest(unittest.TestCase):
    def test_accepted_subscription_is_logged(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        response = Response(200, "OK",
                            headers={"Call-ID": client.dialogs[0].call_id})
        self.assertTrue(manager.handle_response(response))
        self.assertIn("SUBSCRIBE 101 accepted (200 OK)", log)

    def test_rejected_subscription_is_logged_and_state_stays_unknown(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        response = Response(404, "Not Found",
                            headers={"Call-ID": client.dialogs[0].call_id})
        self.assertTrue(manager.handle_response(response))
        self.assertIn("SUBSCRIBE 101 rejected: 404 Not Found", log)
        self.assertEqual(manager.get("101").state, "unknown")

    def test_rejection_is_reported_once_per_response(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_response(
            Response(503, "Service Unavailable",
                     headers={"Call-ID": client.dialogs[1].call_id}))
        hits = [line for line in log.lines if "rejected" in line]
        self.assertEqual(len(hits), 1)
        self.assertIn("SUBSCRIBE 102 rejected: 503", hits[0])

    def test_call_traffic_is_not_claimed(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        stray = Response(200, "OK", headers={"Call-ID": "some-call-dialog"})
        self.assertFalse(manager.handle_response(stray))

    def test_refresh_responses_reuse_the_subscription_call_id(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.force_refresh()
        response = Response(200, "OK",
                            headers={"Call-ID": client.dialogs[0].call_id})
        self.assertTrue(manager.handle_response(response))

    def test_a_request_is_not_a_response(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.assertFalse(manager.handle_response(FakeMessage("BYE")))


if __name__ == "__main__":
    unittest.main()

class NotifyResponseTest(unittest.TestCase):
    """A notifier retransmits until it gets a 2xx, so every NOTIFY is answered."""

    def notify_message(self, body="<event/>",
                       headers=None):
        base = {"Content-Type": "application/blf+xml",
                "Call-ID": "watcher-call",
                "CSeq": "3 NOTIFY",
                "Via": "SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK1",
                "From": "<sip:101@default>;tag=servertag",
                "To": "<sip:100@127.0.0.1>;tag=watchertag"}
        base.update(headers or {})
        return FakeMessage("NOTIFY", body=body, headers=base)

    def test_notify_is_answered_with_200_ok(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(self.notify_message())
        self.assertEqual(len(client.sent), 1)
        self.assertIn(b"SIP/2.0 200 OK", client.sent[0])

    def test_response_echoes_the_dialog_headers(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(self.notify_message())
        sent = client.sent[0]
        self.assertIn(b"Call-ID: watcher-call", sent)
        self.assertIn(b"CSeq: 3 NOTIFY", sent)
        self.assertIn(b"tag=servertag", sent)
        self.assertIn(b"tag=watchertag", sent)
        self.assertIn(b"branch=z9hG4bK1", sent)

    def test_unparseable_body_is_still_answered(self):
        # Nothing to update, but staying silent would make the notifier
        # retransmit the same broken body until the subscription expires.
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(self.notify_message(body="not xml at all"))
        self.assertIn(b"SIP/2.0 200 OK", client.sent[0])

    def test_unexpected_content_type_is_still_answered(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(self.notify_message(
            headers={"Content-Type": "application/simple-message-summary"}))
        self.assertIn(b"SIP/2.0 200 OK", client.sent[0])

    def test_each_notify_is_answered_once(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        for _ in range(3):
            manager.handle_notify(self.notify_message())
        self.assertEqual(len(client.sent), 3)

    def test_a_non_notify_is_not_answered(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.handle_notify(FakeMessage("INVITE"))
        self.assertEqual(client.sent, [])


class SubscriptionDialogTagTest(unittest.TestCase):
    """A refresh or unsubscribe without a To tag is a new dialog, not a refresh."""

    def accepted(self, manager, name="101", to_tag="serverTAG"):
        call_id = None
        for dialog_name, dialog in manager._dialogs.items():
            if dialog_name == name:
                call_id = dialog.call_id
        manager.handle_response(Response(
            200, "OK", headers={"Call-ID": call_id,
                               "To": f"<sip:101@default>;tag={to_tag}"}))

    def test_accepted_subscribe_records_the_to_tag(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.accepted(manager)
        self.assertEqual(manager._dialogs["101"].to_tag, "serverTAG")

    def test_refresh_carries_the_to_tag(self):
        client, manager, log = make_manager(refresh=0)
        manager.subscribe_all()
        self.accepted(manager)
        manager._next_refresh["101"] = time.time() - 1
        manager.refresh_due()
        self.assertEqual(client.dialogs[0].refreshes, [(3600, "serverTAG")])

    def test_unsubscribe_carries_the_to_tag(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        self.accepted(manager)
        manager.unsubscribe_all()
        self.assertEqual(client.dialogs[0].unsubscribes, ["serverTAG"])

    def test_response_without_a_tag_leaves_it_unset(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        call_id = manager._dialogs["101"].call_id
        manager.handle_response(Response(
            200, "OK", headers={"Call-ID": call_id,
                               "To": "<sip:101@default>"}))
        self.assertIsNone(manager._dialogs["101"].to_tag)


class TerminatedSubscriptionTest(unittest.TestCase):
    """Subscription-State is how the server says the subscription is over.

    Ignoring it left the button frozen on its last state with no log, and the
    recovery SUBSCRIBE went out on the dead dialog, so the server rejected it
    on every refresh tick forever.
    """

    def notify(self, state, reason=None):
        value = state if reason is None else f"{state};reason={reason}"
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<event name="dialog" xmlns="urn:freedeswitch:blf">'
                '<instance name="101@default"><params>'
                '<param name="rpid" value="ringing"/>'
                "</params></instance></event>")
        return FakeMessage("NOTIFY", body=body, headers={
            "Content-Type": "application/blf+xml",
            "Subscription-State": value,
            "Call-ID": "watcher",
            "CSeq": "4 NOTIFY",
            "Via": "v", "From": "f", "To": "t"})

    def setUp(self):
        self.client, self.manager, self.log = make_manager()
        self.manager.subscribe_all()
        self.manager.handle_notify(FakeMessage(
            "NOTIFY",
            body='<?xml version="1.0"?><event name="dialog" '
                 'xmlns="urn:freedeswitch:blf"><instance name="101@default">'
                 '<params><param name="rpid" value="ringing"/></params>'
                 "</instance></event>",
            headers={"Content-Type": "application/blf+xml", "Call-ID": "w",
                     "CSeq": "1 NOTIFY", "Via": "v", "From": "f", "To": "t"}))
        self.assertEqual(self.manager.get("101").state, "ringing")

    def test_terminated_is_reported(self):
        self.manager.handle_notify(self.notify("terminated", "timeout"))
        self.assertIn("terminated", self.log)

    def test_terminated_clears_the_frozen_state(self):
        self.manager.handle_notify(self.notify("terminated", "timeout"))
        self.assertNotEqual(self.manager.get("101").state, "ringing")

    def test_terminated_opens_a_new_dialog(self):
        self.manager.handle_notify(self.notify("terminated", "timeout"))
        self.assertGreaterEqual(len(self.client.dialogs), 3)

    def test_active_state_is_not_logged_as_a_problem(self):
        self.manager.handle_notify(self.notify("active"))
        self.assertNotIn("terminated", self.log)
