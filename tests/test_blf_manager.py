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

    def send_subscribe(self, event, accept, expires, event_id):
        self.subscribes.append((event, accept, expires, event_id))

    def send_refresh(self, expires):
        self.refreshes.append(expires)

    def send_unsubscribe(self):
        self.unsubscribes.append(True)


class FakeClient:
    def __init__(self):
        self.dialogs = []

    def create_dialog(self, target_uri):
        dialog = FakeDialog(target_uri)
        self.dialogs.append(dialog)
        return dialog


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

    def test_resubscribing_reuses_the_same_dialog(self):
        client, manager, log = make_manager()
        manager.subscribe_all()
        manager.subscribe_all()
        self.assertEqual(len(client.dialogs), 2)
        self.assertEqual(client.dialogs[0].subscribes.__len__(), 1)
        self.assertEqual(len(client.dialogs[0].refreshes), 1)

    def test_refresh_reuses_the_original_expires(self):
        # A re-SUBSCRIBE re-sends the original Expires. blf.refresh only
        # decides WHEN the client re-subscribes; it is never the Expires value.
        client, manager, log = make_manager(refresh=1800, expires=3600)
        manager.subscribe_all()
        manager.subscribe_all()
        self.assertEqual(client.dialogs[0].refreshes, [3600])

    def test_refresh_due_refreshes_only_expired_buttons(self):
        client, manager, log = make_manager(refresh=1800, expires=3600)
        manager.subscribe_all()
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