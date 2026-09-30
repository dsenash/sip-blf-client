import unittest

from call_session import CallSession, extract_tag, parse_sip_uri
from sip_client import SIPHeaders


class ParseSipUriTest(unittest.TestCase):
    def test_parses_user_host_and_port(self):
        self.assertEqual(parse_sip_uri("sip:101@192.168.0.89:5060"),
                         {"user": "101", "host": "192.168.0.89",
                          "port": 5060, "transport": None})

    def test_defaults_the_port(self):
        parsed = parse_sip_uri("sip:101@192.168.0.89")
        self.assertEqual(parsed["port"], 5060)
        self.assertEqual(parsed["host"], "192.168.0.89")

    def test_reads_the_transport(self):
        parsed = parse_sip_uri("sip:101@192.168.0.89:5060;transport=tcp")
        self.assertEqual(parsed["transport"], "tcp")

    def test_lowercases_the_transport(self):
        parsed = parse_sip_uri("sip:101@h:5060;transport=TCP")
        self.assertEqual(parsed["transport"], "tcp")

    def test_strips_the_scheme(self):
        parsed = parse_sip_uri("101@192.168.0.89:5060")
        self.assertEqual(parsed["user"], "101")

    def test_trims_surrounding_whitespace(self):
        parsed = parse_sip_uri("  sip:101@192.168.0.89:5060  ")
        self.assertEqual(parsed["user"], "101")

    def test_falls_back_to_5060_for_a_non_numeric_port(self):
        parsed = parse_sip_uri("sip:101@host:port")
        self.assertEqual(parsed["port"], 5060)
        self.assertEqual(parsed["host"], "host")

    def test_missing_user_is_empty(self):
        parsed = parse_sip_uri("sip:192.168.0.89")
        self.assertEqual(parsed["user"], "")
        self.assertEqual(parsed["host"], "192.168.0.89")


class Response:
    def __init__(self, code, reason, headers=None, body=""):
        self.method = None
        self.status_code = code
        self.reason = reason
        self.headers = SIPHeaders(headers or {})
        self.body = body


class Request:
    def __init__(self, method, headers=None, body=""):
        self.method = method
        self.status_code = None
        self.reason = None
        self.headers = SIPHeaders(headers or {})
        self.body = body


class FakeDialog:
    def __init__(self, target_uri):
        self.target_uri = target_uri
        self.call_id = f"call-{target_uri}"
        self.invites = []
        self.acks = []
        self.byes = []
        self.cancels = []

    def send_invite(self):
        self.invites.append(True)

    def send_ack(self, to_tag=None):
        self.acks.append(to_tag)

    def send_bye(self, to_tag=None):
        self.byes.append(to_tag)

    def send_cancel(self):
        self.cancels.append(True)


class FakeClient:
    def __init__(self):
        self.dialogs = []
        self.sent = []
        self.last_sdp = ""
        self.remote_addr = ("10.0.0.1", 5060)

    def create_dialog(self, target_uri):
        dialog = FakeDialog(target_uri)
        self.dialogs.append(dialog)
        return dialog

    def send(self, data):
        self.sent.append(data)

    def parse_sdp(self, body):
        return {"media_port": None, "media_ip": None, "codecs": []}


class RecordingLog:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    def __contains__(self, fragment):
        return any(fragment in line for line in self.lines)


def make_session(**overrides):
    config = {"local_ip": "192.168.0.10", "sip_call_timeout": 30}
    config.update(overrides)
    client = FakeClient()
    log = RecordingLog()
    session = CallSession(client, config, 10000, "test.wav", 5,
                          log=log, media_enabled=False)
    return client, session, log


def make_response(code, reason, call_id="call-sip:101@10.0.0.1:5060"):
    return Response(code, reason, headers={"Call-ID": call_id})


def ok_response(call_id="call-sip:101@10.0.0.1:5060"):
    return Response(200, "OK", headers={
        "To": "<sip:101@x>;tag=remoteTAG", "Call-ID": call_id})


def bye_request(client):
    """A BYE the server would send on the session's own dialog."""
    return Request("BYE", headers={
        **BYE_HEADERS, "Call-ID": client.dialogs[0].call_id})


def reinvite_request(client):
    return Request("INVITE", headers={
        **BYE_HEADERS, "Call-ID": client.dialogs[0].call_id})


BYE_HEADERS = {
    "Via": "SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK1",
    "From": "<sip:101@10.0.0.1>;tag=abc",
    "To": "<sip:100@192.168.0.10>;tag=def",
    "Call-ID": "server-call-id",
    "CSeq": "2 BYE",
}


class ExtractTagTest(unittest.TestCase):
    def test_reads_the_tag(self):
        self.assertEqual(extract_tag("<sip:101@x>;tag=abc"), "abc")

    def test_takes_the_first_of_several_parameters(self):
        self.assertEqual(extract_tag("<sip:101@x>;tag=abc;expires=60"), "abc")

    def test_missing_tag_is_none(self):
        self.assertIsNone(extract_tag("<sip:101@x>"))

    def test_missing_header_is_none(self):
        self.assertIsNone(extract_tag(""))


class StartTest(unittest.TestCase):
    def test_starts_a_call(self):
        client, session, log = make_session()
        self.assertTrue(session.start("sip:101@10.0.0.1:5060"))
        self.assertEqual(session.state, CallSession.CALLING)
        self.assertEqual(session.uri, "sip:101@10.0.0.1:5060")

    def test_start_opens_one_dialog_and_sends_invite(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        self.assertEqual(len(client.dialogs), 1)
        self.assertEqual(client.dialogs[0].invites, [True])

    def test_start_refuses_while_a_call_is_active(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        self.assertFalse(session.start("sip:102@10.0.0.1:5060"))
        self.assertEqual(len(client.dialogs), 1)

    def test_start_refuses_while_calling(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        self.assertFalse(session.start("sip:102@10.0.0.1:5060"))

    def test_start_is_allowed_again_after_the_call_ends(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.hangup()
        self.assertTrue(session.start("sip:102@10.0.0.1:5060"))


class ResponseHandlingTest(unittest.TestCase):
    def test_200_answers_with_ack_and_becomes_active(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        self.assertEqual(session.state, CallSession.ACTIVE)
        self.assertEqual(client.dialogs[0].acks, ["remoteTAG"])

    def test_1xx_is_ignored(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        self.assertFalse(session.on_message(make_response(100, "Trying")))
        self.assertEqual(session.state, CallSession.CALLING)

    def test_4xx_finishes_the_call(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(make_response(404, "Not Found"))
        self.assertEqual(session.state, CallSession.DONE)
        self.assertIn("Call failed: 404", log)

    def test_5xx_finishes_the_call(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(make_response(503, "Service Unavailable"))
        self.assertEqual(session.state, CallSession.DONE)

    def test_3xx_is_not_followed(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(make_response(302, "Moved Temporarily"))
        self.assertEqual(session.state, CallSession.DONE)
        self.assertIn("Redirection", log)

    def test_response_while_idle_is_ignored(self):
        client, session, log = make_session()
        self.assertFalse(session.on_message(ok_response()))
        self.assertEqual(session.state, CallSession.IDLE)

    def test_bye_is_answered_and_finishes(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(bye_request(client))
        self.assertEqual(session.state, CallSession.DONE)
        self.assertEqual(len(client.sent), 1)
        self.assertIn(b"SIP/2.0 200 OK", client.sent[0])
        self.assertIn(f"Call-ID: {client.dialogs[0].call_id}".encode(),
                      client.sent[0])

    def test_invite_while_idle_is_ignored(self):
        client, session, log = make_session()
        self.assertFalse(session.on_message(
            Request("INVITE", headers=dict(BYE_HEADERS))))
        self.assertEqual(client.sent, [])

    def test_reinvite_while_active_is_answered(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        self.assertTrue(session.on_message(
            reinvite_request(client)))
        self.assertIn(b"SIP/2.0 200 OK", client.sent[0])

    def test_finished_stays_finished(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(make_response(404, "Not Found"))
        self.assertTrue(session.finished)
        self.assertFalse(session.on_message(ok_response()))


class HangupAndTimeoutTest(unittest.TestCase):
    def test_hangup_sends_bye_when_active(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.hangup()
        self.assertEqual(client.dialogs[0].byes, ["remoteTAG"])
        self.assertEqual(session.state, CallSession.DONE)

    def test_hangup_while_calling_sends_no_bye(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.hangup()
        self.assertEqual(client.dialogs[0].byes, [])
        self.assertEqual(session.state, CallSession.DONE)

    def test_hangup_when_idle_is_a_no_op(self):
        client, session, log = make_session()
        session.hangup()
        self.assertEqual(session.state, CallSession.IDLE)
        self.assertEqual(client.dialogs, [])

    def test_hangup_twice_sends_one_bye(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.hangup()
        session.hangup()
        self.assertEqual(len(client.dialogs[0].byes), 1)

    def test_tick_finishes_a_timed_out_call(self):
        _, session, log = make_session(sip_call_timeout=0)
        session.start("sip:101@10.0.0.1:5060")
        session.tick()
        self.assertEqual(session.state, CallSession.DONE)
        self.assertIn("Timeout", log)

    def test_tick_does_nothing_while_active(self):
        _, session, log = make_session(sip_call_timeout=0)
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.tick()
        self.assertEqual(session.state, CallSession.ACTIVE)

    def test_tick_does_nothing_while_idle(self):
        _, session, log = make_session(sip_call_timeout=0)
        session.tick()
        self.assertEqual(session.state, CallSession.IDLE)

    def test_close_hangs_up_an_active_call(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.close()
        self.assertEqual(client.dialogs[0].byes, ["remoteTAG"])


if __name__ == "__main__":
    unittest.main()

class ForeignDialogTest(unittest.TestCase):
    """A response or request for another Call-ID must not touch this call.

    The same session object is reused for a second call, so a late answer from
    the first one used to be accepted: the session ACKed with the dead dialog's
    To tag, adopted its SDP, and then discarded the real answer because the
    state was no longer CALLING. The call wedged and was never hung up.
    """

    def test_response_for_another_dialog_is_ignored(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        self.assertFalse(session.on_message(ok_response(call_id="some-other-call")))
        self.assertEqual(session.state, CallSession.CALLING)
        self.assertEqual(client.dialogs[0].acks, [])

    def test_rejection_for_another_dialog_is_ignored(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(Response(404, "Not Found",
                                    headers={"Call-ID": "some-other-call"}))
        self.assertEqual(session.state, CallSession.CALLING)

    def test_bye_for_another_dialog_does_not_end_the_call(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.on_message(Request("BYE", headers={
            **BYE_HEADERS, "Call-ID": "some-other-call"}))
        self.assertEqual(session.state, CallSession.ACTIVE)
        self.assertEqual(client.sent, [])

    def test_reinvite_for_another_dialog_is_not_answered(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.on_message(Request("INVITE", headers={
            **BYE_HEADERS, "Call-ID": "some-other-call"}))
        self.assertEqual(client.sent, [])

    def test_bye_for_this_dialog_is_still_answered(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.on_message(Request("BYE", headers={
            **BYE_HEADERS, "Call-ID": client.dialogs[0].call_id}))
        self.assertEqual(session.state, CallSession.DONE)
        self.assertEqual(len(client.sent), 1)

    def test_late_answer_after_a_timeout_does_not_reopen_the_session(self):
        client, session, log = make_session(sip_call_timeout=0)
        session.start("sip:101@10.0.0.1:5060")
        session.tick()
        self.assertEqual(session.state, CallSession.DONE)
        # A second call reuses the session object with a fresh dialog.
        self.assertTrue(session.start("sip:102@10.0.0.1:5060"))
        self.assertFalse(session.on_message(
            ok_response(call_id=client.dialogs[0].call_id)))
        self.assertEqual(session.state, CallSession.CALLING)
        self.assertEqual(client.dialogs[1].acks, [])


class CancelTest(unittest.TestCase):
    """Abandoning a ringing call without a CANCEL leaves the far end ringing."""

    def test_hangup_while_calling_sends_a_cancel(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.hangup()
        self.assertEqual(client.dialogs[0].cancels, [True])
        self.assertEqual(session.state, CallSession.DONE)

    def test_cancel_without_an_invite_sends_nothing(self):
        client, session, log = make_session()
        session.hangup()
        self.assertEqual(session.state, CallSession.IDLE)
        self.assertEqual(client.dialogs, [])

    def test_hangup_while_calling_still_sends_no_bye(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.hangup()
        self.assertEqual(client.dialogs[0].byes, [])

    def test_timeout_while_calling_sends_a_cancel(self):
        client, session, log = make_session(sip_call_timeout=0)
        session.start("sip:101@10.0.0.1:5060")
        session.tick()
        self.assertEqual(client.dialogs[0].cancels, [True])

    def test_close_while_calling_sends_a_cancel(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.close()
        self.assertEqual(client.dialogs[0].cancels, [True])

    def test_an_answered_call_sends_a_bye_not_a_cancel(self):
        client, session, log = make_session()
        session.start("sip:101@10.0.0.1:5060")
        session.on_message(ok_response())
        session.hangup()
        self.assertEqual(client.dialogs[0].byes, ["remoteTAG"])
        self.assertEqual(client.dialogs[0].cancels, [])
