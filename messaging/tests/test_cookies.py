from daimon_messages import cookies

JAR = "SID=a1; HSID=b2; SSID=c3; OSID=d4; APISID=e5; SAPISID=f6/x=; __Secure-1PSIDTS=g7"
WANT = {"SID": "a1", "HSID": "b2", "SSID": "c3", "OSID": "d4", "APISID": "e5", "SAPISID": "f6/x=",
        "__Secure-1PSIDTS": "g7"}


def test_curl_bash():
    text = (f"curl 'https://instantmessaging-pa.googleapis.com/web/config' \\\n  -H 'accept: */*' \\\n"
            f"  -H 'cookie: {JAR}' \\\n  -H 'user-agent: Mozilla/5.0'")
    assert cookies.parse(text) == WANT


def test_curl_b_flag():
    assert cookies.parse(f"curl 'https://x' -b '{JAR}' -H 'accept: */*'") == WANT


def test_curl_cmd():
    assert cookies.parse(f'curl ^"https://x^" ^\n  -H ^"cookie: {JAR}^" ^\n  -H ^"accept: */*^"') == WANT


def test_powershell():
    text = "\n".join(f'$session.Cookies.Add((New-Object System.Net.Cookie("{k}", "{v}", "/", ".google.com")))'
                     for k, v in WANT.items())
    assert cookies.parse(text) == WANT


def test_header_and_json():
    assert cookies.parse(f"Cookie: {JAR}") == WANT
    assert cookies.parse(JAR) == WANT
    assert cookies.parse('{"SID": "a1", "HSID": "b2"}') == {"SID": "a1", "HSID": "b2"}


def test_missing():
    assert cookies.missing(WANT) == []
    assert cookies.missing({"SID": "a1"}) == ["HSID", "SSID", "OSID", "APISID", "SAPISID"]
    assert cookies.parse("nothing here") == {}


def test_cut_off_paste():
    text = f"curl 'https://x' \\\n  -b '{JAR}' \\\n  -H 'accept: */*'"
    try:
        cookies.parse(text[:text.index("HSID")])
    except cookies.CutOff:
        pass
    else:
        raise AssertionError("a truncated paste should raise CutOff")


def test_ansi_c_quoting():
    assert cookies.parse(f"curl 'https://x' -b $'{JAR}'") == WANT
