import time
import unittest
from xml.etree import ElementTree

from blf import (BLFButton, BLFParseError, extract_state, format_change,
                  parse_blf_xml)

NS = 'xmlns="urn:freedeswitch:blf"'


def body(inner):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<event name="dialog" {NS}>{inner}</event>'


def instance(name, rpid, caller_number=None, caller_name=None, uuid=None):
    params = [f'<param name="rpid" value="{rpid}"/>']
    if caller_number:
        params.append(f'<param name="caller_id_number" value="{caller_number}"/>')
    if caller_name:
        params.append(f'<param name="caller_id_name" value="{caller_name}"/>')
    if uuid:
        params.append(f'<param name="uuid" value="{uuid}"/>')
    return f'<instance name="{name}"><params>{"".join(params)}</params></instance>'


def button(**overrides):
    attrs = {"name": "101", "number": "101", "domain": "default",
             "label": "Operator 1"}
    attrs.update(overrides)
    return BLFButton(attrs.pop("name"), attrs.pop("number"),
                     attrs.pop("domain"), attrs.pop("label", None))


class ParseBlfXmlTest(unittest.TestCase):
    def test_parses_namespaced_instance(self):
        raw = body(instance("101@default", "ringing"))
        parsed = parse_blf_xml(raw)
        self.assertIn("101@default", parsed)
        self.assertEqual(parsed["101@default"]["rpid"], "ringing")

    def test_parses_several_instances(self):
        raw = body(instance("101@default", "idle")
                   + instance("102@default", "busy"))
        parsed = parse_blf_xml(raw)
        self.assertEqual(sorted(parsed), ["101@default", "102@default"])
        self.assertEqual(parsed["102@default"]["rpid"], "busy")

    def test_params_without_a_wrapper_are_also_read(self):
        raw = body('<instance name="101@default">'
                   '<param name="rpid" value="held"/></instance>')
        self.assertEqual(parse_blf_xml(raw)["101@default"]["rpid"], "held")

    def test_ignores_unknown_elements(self):
        raw = body('<custom>noise</custom>'
                   + instance("101@default", "idle"))
        self.assertEqual(parse_blf_xml(raw)["101@default"]["rpid"], "idle")

    def test_instance_without_a_name_is_skipped(self):
        raw = body('<instance><params>'
                   '<param name="rpid" value="idle"/></params></instance>')
        self.assertEqual(parse_blf_xml(raw), {})

    def test_malformed_xml_raises(self):
        with self.assertRaises(BLFParseError):
            parse_blf_xml("<event><instance></event>")

    def test_empty_body_raises(self):
        with self.assertRaises(BLFParseError):
            parse_blf_xml("")


class ExtractStateTest(unittest.TestCase):
    def test_reads_rpid(self):
        self.assertEqual(extract_state({"rpid": "ringing"}), "ringing")

    def test_is_case_insensitive(self):
        self.assertEqual(extract_state({"rpid": " Ringing "}), "ringing")

    def test_accepts_the_rpid_value_alias(self):
        self.assertEqual(extract_state({"rpid_value": "busy"}), "busy")

    def test_unknown_value_becomes_unknown(self):
        self.assertEqual(extract_state({"rpid": "dancing"}), "unknown")

    def test_missing_state_becomes_unknown(self):
        self.assertEqual(extract_state({}), "unknown")
        self.assertEqual(extract_state({"caller_id_number": "555"}), "unknown")


class BLFButtonTest(unittest.TestCase):
    def test_label_defaults_to_number(self):
        self.assertEqual(button(label=None).label, "101")

    def test_subscribe_uri_targets_the_domain(self):
        self.assertEqual(button().subscribe_uri, "sip:101@default")

    def test_starts_unknown_with_no_caller(self):
        b = button()
        self.assertEqual(b.state, "unknown")
        self.assertIsNone(b.caller_number)
        self.assertIsNone(b.caller_name)
        self.assertIsNone(b.uuid)
        self.assertEqual(b.previous_state, "unknown")

    def test_apply_sets_state_and_caller(self):
        b = button()
        self.assertTrue(b.apply({"rpid": "ringing",
                                 "caller_id_number": "5551234",
                                 "caller_id_name": "Ann",
                                 "uuid": "abc-123"}))
        self.assertEqual(b.state, "ringing")
        self.assertEqual(b.caller_number, "5551234")
        self.assertEqual(b.caller_name, "Ann")
        self.assertEqual(b.uuid, "abc-123")
        self.assertEqual(b.previous_state, "unknown")

    def test_apply_unchanged_reports_false(self):
        b = button()
        b.apply({"rpid": "ringing", "caller_id_number": "5551234"})
        self.assertFalse(b.apply({"rpid": "ringing",
                                  "caller_id_number": "5551234"}))

    def test_apply_clears_caller_when_absent(self):
        b = button()
        b.apply({"rpid": "ringing", "caller_id_number": "5551234"})
        self.assertTrue(b.apply({"rpid": "idle"}))
        self.assertEqual(b.state, "idle")
        self.assertIsNone(b.caller_number)

    def test_apply_caller_change_alone_counts_as_a_change(self):
        b = button()
        b.apply({"rpid": "ringing", "caller_id_number": "111"})
        self.assertTrue(b.apply({"rpid": "ringing", "caller_id_number": "222"}))
        self.assertEqual(b.caller_number, "222")

    def test_changed_at_moves_on_a_change(self):
        b = button()
        before = time.time()
        b.apply({"rpid": "ringing"})
        self.assertGreaterEqual(b.changed_at, before)


class FormatChangeTest(unittest.TestCase):
    def test_line_starts_with_the_blf_prefix(self):
        b = button()
        b.apply({"rpid": "ringing"})
        self.assertTrue(format_change(b).startswith("[BLF] "))

    def test_shows_previous_and_current_state(self):
        b = button()
        b.apply({"rpid": "ringing"})
        self.assertIn("101 (Operator 1): unknown -> ringing", format_change(b))

    def test_includes_caller_when_present(self):
        b = button()
        b.apply({"rpid": "ringing", "caller_id_number": "5551234",
                 "caller_id_name": "Ann"})
        line = format_change(b)
        self.assertIn('caller="Ann" <5551234>', line)

    def test_tolerates_a_missing_caller_name(self):
        b = button()
        b.apply({"rpid": "ringing", "caller_id_number": "5551234"})
        line = format_change(b)
        self.assertIn("caller=<5551234>", line)
        self.assertNotIn('""', line)

    def test_tolerates_a_missing_caller_number(self):
        b = button()
        b.apply({"rpid": "ringing", "caller_id_name": "Ann"})
        line = format_change(b)
        self.assertIn('caller="Ann"', line)
        self.assertNotIn("<>", line)

    def test_omits_caller_entirely_when_absent(self):
        b = button()
        b.apply({"rpid": "idle"})
        self.assertNotIn("caller=", format_change(b))

    def test_includes_uuid_when_present(self):
        b = button()
        b.apply({"rpid": "ringing", "uuid": "abc-123"})
        self.assertIn("uuid=abc-123", format_change(b))


class RoundTripTest(unittest.TestCase):
    def test_a_generated_body_parses_back_to_the_same_state(self):
        raw = body(instance("101@default", "ringing", caller_number="5551234"))
        parsed = parse_blf_xml(raw)
        b = button()
        b.apply(parsed["101@default"])
        self.assertEqual(b.state, "ringing")
        self.assertEqual(b.caller_number, "5551234")

    def test_a_caller_name_with_markup_survives_as_text(self):
        raw = body(instance("101@default", "ringing",
                            caller_number="5551234",
                            caller_name="Ann &amp; Bob"))
        parsed = parse_blf_xml(raw)
        b = button()
        b.apply(parsed["101@default"])
        self.assertEqual(b.caller_name, "Ann & Bob")

    def test_elementtree_accepts_our_namespace_prefix(self):
        raw = ('<event xmlns:fs="urn:freedeswitch:blf">'
               '<fs:instance name="101@default"><fs:params>'
               '<fs:param name="rpid" value="busy"/>'
               '</fs:params></fs:instance></event>')
        parsed = parse_blf_xml(raw)
        self.assertEqual(parsed["101@default"]["rpid"], "busy")


if __name__ == "__main__":
    unittest.main()