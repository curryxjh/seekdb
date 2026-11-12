"""Exercise decimal-to-double-to-float boundaries through the official JSONL client."""
import argparse
from pathlib import Path
import struct
import tempfile

from client import Client, sql_string


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def run(binary):
    valid = [
        "0", "+0", "-0", "0.", ".0", "+.5", "-.5", "1.", "+1.", "-1.",
        "01.2500", "+0003.50e+0", "6.25E-1", "2.e+3", ".5e-1", "-1.E-2",
        "16777217", "16777219", "9007199254740993",
        # Direct decimal-to-float parsing rounds these differently.
        "1.0000000596046448", "-1.0000000596046448", "1.0000001788139342",
        "3.40282346638528859811704183484516925440e38",
        "-3.40282346638528859811704183484516925440e38",
        "3.4028235677973363e38",
        "1.17549435082228750796873653722224567782e-38",
        "1.40129846432481707092372958328991613128e-45",
        "7.00649232162408535461864791644958065640e-46",
        "7.006492321624087e-46", "-7.006492321624087e-46",
        "1e-46", "-1e-46", "2.2250738585072014e-308",
        "2.225073858507201e-308", "5e-324", "-5e-324",
        "2.4703282292062327e-324", "2.4703282292062328e-324",
        "1e-999", "-1e-999", "1e-999999999999999999999",
        "-1e-999999999999999999999", "0e999999999999999999999",
        "-0e999999999999999999999", "+0e-999999999999999999999",
        "0." + "0" * 400 + "1e400", "1" + "0" * 400 + "e-400",
        "1." + "0" * 1024,
    ]
    invalid_numbers = [
        "", "+", "-", ".", "+.", "-.", "++1", "--1", "+-1", "-+1",
        "1e", "1e+", "1e-", ".e1", "1.e", "1e+-1", "1e--1", "1e1e1",
        "1..0", "1.2.3", "1.0tail", "1_0", "1e1.0", "0x1", "0x1p0",
        "NaN", "nan", "+nan", "nan(1)", "Inf", "Infinity", "-infinity",
        "1e39", "-1e39", "3.4028235677973365e38", "1.7976931348623157e308",
        "1.7976931348623159e308", "1e309", "-1e309", "1e999",
        "1e999999999999999999999", "-1e999999999999999999999",
        "true", "null", '"1"', "1\x00", "1\v", "1\u00a0", "１",
    ]
    invalid_arrays = [
        "", "0", "[", "]", "[]", "[1", "1]", "[1,]", "[,1]", "[1,,1]",
        "[1 1]", "[1\t1]", "[1;1]", "[1,2]", "[[1]]", "[1]]", "[1]tail",
        "[1] [1]", "[1] ,", "\v[1]", "[\v1]", "[1]\x00",
    ]
    checks = 0
    with tempfile.TemporaryDirectory(prefix="mvdb-vector-numbers-") as directory:
        client = Client([binary], directory)
        try:
            client.ok("CREATE TABLE numbers(id BIGINT PRIMARY KEY,v VECTOR(1));")
            client.ok("BEGIN;")
            expected = []
            for key, token in enumerate(valid):
                client.ok(f"INSERT INTO numbers VALUES({key},{sql_string('[' + token + ']')});")
                expected.append([key, [f32(float(token))]])
                checks += 1
            client.ok("COMMIT;")

            def verify_rows():
                nonlocal checks
                rows = client.ok("SELECT id,v FROM numbers ORDER BY id;")["rows"]
                assert rows == expected, (rows, expected)
                # JSON may encode signed zero as the integer -0; compare other bits exactly.
                for (_, actual), (_, wanted) in zip(rows, expected):
                    if wanted[0] != 0:
                        assert struct.pack("<f", actual[0]) == struct.pack("<f", wanted[0])
                checks += len(rows)

            verify_rows()
            for vector in [f"[{token}]" for token in invalid_numbers] + invalid_arrays:
                result = client.sql(f"INSERT INTO numbers VALUES(999,{sql_string(vector)});")
                assert result.get("ok") is False, (vector, result)
                assert result["error"]["code"] == "invalid_vector", (vector, result)
                checks += 1
            verify_rows()

            # Both bracket SQL literals and quoted arrays reach vector_literal.
            whitespace = sql_string(" \t\r\n\f[ \t+.5e+1 \r\n\f] \t\r\n\f")
            client.ok(f"UPDATE numbers SET v={whitespace} WHERE id=0;")
            assert client.ok("SELECT v FROM numbers WHERE id=0;")["rows"] == [[[5.0]]]
            client.ok("CREATE TABLE mixed(id BIGINT PRIMARY KEY,v VECTOR(4));")
            client.ok("INSERT INTO mixed VALUES(1,[+.5,-.5,1.,-2E+1]);")
            assert client.ok("SELECT v FROM mixed;")["rows"] == [[[0.5, -0.5, 1.0, -20.0]]]
            client.ok("UPDATE numbers SET v=[+.5e+1] WHERE id=0;")
            expected[0] = [0, [5.0]]
            distance = client.ok(
                "SELECT L2_DISTANCE(v,'[1.0000000596046448]') AS d "
                "FROM numbers WHERE id=0;")["rows"]
            assert distance == [[4.0]], distance
            for token in ["+", "1e+", "1e999", "1e39", "nan", "0x1p0"]:
                result = client.sql(
                    f"SELECT L2_DISTANCE(v,{sql_string('[' + token + ']')}) "
                    "FROM numbers WHERE id=0;")
                assert result.get("ok") is False, (token, result)
                assert result["error"]["code"] == "invalid_vector", (token, result)
                checks += 1
            checks += 6
            verify_rows()
            client.close()
            client = Client([binary], directory)
            verify_rows()
        finally:
            client.close()
    print(f"PASS vector numbers: {checks} checks; double-to-float rounding, signs, "
          "subnormals/underflow, overflow, strict syntax, query literals and restart")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    args = parser.parse_args()
    run(str(Path(args.binary).resolve()))
