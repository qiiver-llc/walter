from walter.net import parse_wifi_list, split_nmcli


def test_split_escaped_colons():
    assert split_nmcli(r"*:My\:Net:80:WPA2") == ["*", "My:Net", "80", "WPA2"]


def test_parse_dedupes_and_sorts():
    out = "\n".join([
        ":Cafe:40:WPA2",
        ":Cafe:72:WPA2",
        "*:Home:55:WPA2 WPA3",
        ":Open Net:90:",
        "::30:WPA2",          # hidden network, skipped
    ])
    nets = parse_wifi_list(out)
    assert [n["ssid"] for n in nets] == ["Home", "Open Net", "Cafe"]
    assert nets[0]["in_use"] and nets[0]["secure"]
    assert nets[1]["secure"] is False
    assert nets[2]["signal"] == 72
