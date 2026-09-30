import unittest
from xml.etree import ElementTree

from mock_server import BLF_SCRIPT, make_notify, make_notify_body


def instances_of(body):
    root = ElementTree.fromstring(body)
    return [n for n in root.iter() if n.tag.endswith("instance")]


def params_of(body):
    return {p.get("name"): p.get("value")
            for p in ElementTree.fromstring(body).iter()
            if p.tag.endswith("param")}


class MakeNotifyBodyTest(unittest.TestCase):
    def test_body_parses_and_carries_rpid(self):
        body = make_notify_body([("101@default", "ringing", "5551234")])
        instances = instances_of(body)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].get("name"), "101@default")
        params = params_of(body)
        self.assertEqual(params["rpid"], "ringing")
        self.assertEqual(params["caller_id_number"], "5551234")

    def test_body_with_no_instances_is_still_valid_xml(self):
        root = ElementTree.fromstring(make_notify_body([]))
        self.assertTrue(root.tag.endswith("event"))

    def test_body_escapes_xml_in_values(self):
        body = make_notify_body([("101@default", "idle", '"><evil>')])
        # The raw body must carry escaped markup, so a caller_id_number cannot
        # break out of its attribute and inject elements.
        self.assertNotIn("<evil>", body)
        self.assertIn("&lt;evil&gt;", body)
        # Round-tripping restores the original characters as inert text.
        self.assertEqual(params_of(body)["caller_id_number"], '"><evil>')

    def test_body_escapes_the_instance_name(self):
        body = make_notify_body([('1"><evil>@default', "idle", None)])
        self.assertNotIn("<evil>", body)
        self.assertEqual(instances_of(body)[0].get("name"), '1"><evil>@default')

    def test_omits_caller_number_when_absent(self):
        body = make_notify_body([("101@default", "idle", None)])
        self.assertNotIn("caller_id_number", params_of(body))

    def test_body_has_the_blf_namespace(self):
        root = ElementTree.fromstring(make_notify_body([]))
        self.assertEqual(root.get("name"), "dialog")
        self.assertIn("blf", root.tag)

    def test_body_keeps_every_instance(self):
        body = make_notify_body([("101@default", "idle", None),
                                 ("102@default", "ringing", "5551234")])
        names = [i.get("name") for i in instances_of(body)]
        self.assertEqual(names, ["101@default", "102@default"])


class MakeNotifyTest(unittest.TestCase):
    def test_notify_carries_dialog_and_subscription_headers(self):
        raw = make_notify("call-1", "ftag", "ttag", 1, "blf-101@default",
                          make_notify_body([("101@default", "idle", None)]))
        self.assertTrue(raw.startswith("NOTIFY sip:101@default SIP/2.0"))
        self.assertIn("Event: dialog", raw)
        self.assertIn("Subscription-State: active;expires=3600", raw)
        self.assertIn("Event-ID: blf-101@default", raw)
        self.assertIn("Call-ID: call-1", raw)
        self.assertIn(";tag=ftag", raw)
        self.assertIn(";tag=ttag", raw)
        self.assertIn("CSeq: 1 NOTIFY", raw)
        self.assertIn("Content-Type: application/blf+xml", raw)

    def test_notify_declares_the_body_length(self):
        body = make_notify_body([("101@default", "idle", None)])
        raw = make_notify("c", "f", "t", 1, "e", body)
        self.assertIn(f"Content-Length: {len(body.encode())}", raw)

    def test_request_uri_comes_from_the_watched_resource(self):
        # The Request-URI names what the server is watching; the From/To tags
        # name the watcher. Deriving the URI from the tags would broadcast at
        # the wrong dialog.
        body = make_notify_body([("102@default", "idle", None)])
        raw = make_notify("c", "ftag", "ttag", 1, "e", body)
        self.assertTrue(raw.startswith("NOTIFY sip:102@default SIP/2.0"))

    def test_notify_uses_crlf_line_endings(self):
        raw = make_notify("c", "f", "t", 1, "e",
                          make_notify_body([("101@default", "idle", None)]))
        self.assertIn("\r\n", raw)
        self.assertNotIn("\n", raw.replace("\r\n", ""))

    def test_notify_carries_a_from_tag(self):
        raw = make_notify("c", "ftag", "ttag", 1, "e",
                          make_notify_body([("101@default", "idle", None)]))
        self.assertIn("From: <sip:100@127.0.0.1>;tag=ftag", raw)
        self.assertIn("To: <sip:100@127.0.0.1>;tag=ttag", raw)


class BlfScriptTest(unittest.TestCase):
    def test_script_starts_idle_and_ends_idle(self):
        self.assertEqual(BLF_SCRIPT[0][1], "idle")
        self.assertEqual(BLF_SCRIPT[-1][1], "idle")

    def test_script_delays_increase(self):
        delays = [step[0] for step in BLF_SCRIPT]
        self.assertEqual(delays, sorted(delays))

    def test_script_rings_somewhere_in_the_middle(self):
        self.assertIn("ringing", [step[1] for step in BLF_SCRIPT])

    def test_script_steps_are_triples(self):
        for step in BLF_SCRIPT:
            self.assertEqual(len(step), 3)


if __name__ == "__main__":
    unittest.main()