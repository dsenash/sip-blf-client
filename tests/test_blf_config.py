import unittest

from blf import load_buttons, resolve_timing


class RecordingLog:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    def __contains__(self, fragment):
        return any(fragment in line for line in self.lines)


def make_log():
    return RecordingLog()


class LoadButtonsTest(unittest.TestCase):
    def test_reads_name_number_and_label(self):
        config = {"blf": {"domain": "default", "buttons": [
            {"name": "101", "number": "1001", "label": "Operator 1"}]}}
        buttons = load_buttons(config, log=print)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].name, "101")
        self.assertEqual(buttons[0].number, "1001")
        self.assertEqual(buttons[0].label, "Operator 1")
        self.assertEqual(buttons[0].domain, "default")

    def test_number_defaults_to_name(self):
        config = {"blf": {"buttons": [{"name": "101"}]}}
        self.assertEqual(load_buttons(config, log=print)[0].number, "101")

    def test_subscribe_uri_uses_number_and_domain(self):
        config = {"blf": {"domain": "example.com",
                          "buttons": [{"name": "op", "number": "101"}]}}
        self.assertEqual(load_buttons(config, log=print)[0].subscribe_uri,
                         "sip:101@example.com")

    def test_domain_defaults_to_default(self):
        config = {"blf": {"buttons": [{"name": "555"}]}}
        self.assertEqual(load_buttons(config, log=print)[0].domain, "default")

    def test_domain_comes_from_the_section(self):
        config = {"blf": {"domain": "example.com",
                          "buttons": [{"name": "555"}]}}
        self.assertEqual(load_buttons(config, log=print)[0].subscribe_uri,
                         "sip:555@example.com")

    def test_missing_blf_section_yields_no_buttons(self):
        self.assertEqual(load_buttons({}, log=print), [])

    def test_missing_buttons_key_yields_no_buttons(self):
        self.assertEqual(load_buttons({"blf": {"domain": "default"}}, log=print), [])

    def test_button_without_name_is_dropped_with_warning(self):
        log = make_log()
        config = {"blf": {"buttons": [{"number": "101"}, {"name": "102"}]}}
        buttons = load_buttons(config, log=log)
        self.assertEqual([b.name for b in buttons], ["102"])
        self.assertIn("no 'name'", log)

    def test_duplicate_name_is_dropped_with_warning(self):
        log = make_log()
        config = {"blf": {"buttons": [{"name": "101"},
                                       {"name": "101", "number": "999"}]}}
        buttons = load_buttons(config, log=log)
        self.assertEqual([b.number for b in buttons], ["101"])
        self.assertIn("duplicate", log)

    def test_non_object_entry_is_dropped_with_warning(self):
        log = make_log()
        config = {"blf": {"buttons": ["101", {"name": "102"}]}}
        buttons = load_buttons(config, log=log)
        self.assertEqual([b.name for b in buttons], ["102"])
        self.assertIn("not an object", log)

    def test_null_buttons_entry_yields_no_buttons(self):
        self.assertEqual(load_buttons({"blf": {"buttons": None}}, log=print), [])

    def test_empty_button_uses_number_as_label(self):
        config = {"blf": {"buttons": [{"name": "555"}]}}
        self.assertEqual(load_buttons(config, log=print)[0].label, "555")

    def test_order_is_preserved(self):
        config = {"blf": {"buttons": [{"name": "103"}, {"name": "101"},
                                       {"name": "102"}]}}
        self.assertEqual([b.name for b in load_buttons(config, log=print)],
                         ["103", "101", "102"])


class ResolveTimingTest(unittest.TestCase):
    def test_reads_both_values(self):
        self.assertEqual(resolve_timing(
            {"blf": {"expires": 3600, "refresh": 1800}}, log=print),
            (3600, 1800))

    def test_defaults_when_absent(self):
        self.assertEqual(resolve_timing({"blf": {}}, log=print), (3600, 1800))

    def test_defaults_without_a_section(self):
        self.assertEqual(resolve_timing({}, log=print), (3600, 1800))

    def test_strings_are_accepted(self):
        self.assertEqual(resolve_timing(
            {"blf": {"expires": "120", "refresh": "60"}}, log=print), (120, 60))

    def test_refresh_above_expires_is_clamped_with_warning(self):
        log = make_log()
        self.assertEqual(resolve_timing(
            {"blf": {"expires": 600, "refresh": 900}}, log=log), (600, 300))
        self.assertIn("blf.refresh", log)

    def test_refresh_equal_to_expires_is_clamped(self):
        log = make_log()
        self.assertEqual(resolve_timing(
            {"blf": {"expires": 600, "refresh": 600}}, log=log), (600, 300))
        self.assertIn("blf.refresh", log)

    def test_expires_of_one_still_yields_a_positive_refresh(self):
        self.assertEqual(resolve_timing(
            {"blf": {"expires": 1, "refresh": 5}}, log=make_log()), (1, 1))

    def test_no_warning_when_refresh_precedes_expires(self):
        log = make_log()
        resolve_timing({"blf": {"expires": 3600, "refresh": 1800}}, log=log)
        self.assertEqual(log.lines, [])


if __name__ == "__main__":
    unittest.main()