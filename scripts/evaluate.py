#!/usr/bin/env python3
"""Score staged detection observations from two CSV files somebody filled in.

Nothing here talks to a camera, reads a snapshot, exports an image or asks
Frigate anything. It counts what an observer recorded and refuses anything it
cannot read exactly, because a number invented from a file that was misread is
worse than no number at all.

The two files are separate on purpose, because they answer different questions:

  --trials   camera,lighting,expected,detected
             One row per observation somebody actually watched happen.
             camera is exactly camera_1 … camera_4, lighting is exactly day or
             night, and expected and detected are exactly 0 or 1.
  --windows  camera,lighting,hours,false_alerts
             One row per stretch of time somebody watched a camera with nobody
             staged in it. hours is a finite positive number so the rate has a
             denominator; false_alerts is a non-negative whole number.

A false alert is never derived from the trials file. An empty stage looks
exactly like a missed object, and an empty trials file looks exactly like a
model that never fired; only an observer counting against a clock can tell those
apart. With no --windows, the per-camera-hour rate is reported as **unmeasured**,
never as zero.

Stdlib only. No network, no image export, no state, no database.

    python3 scripts/evaluate.py --trials docs/evaluation/trials.csv \\
                               --windows docs/evaluation/windows.csv
    python3 scripts/evaluate.py --self-check

docs/evaluation.md holds the protocol and, more importantly, the list of things
this cannot tell you.
"""

import argparse
import contextlib
import csv
import io
import math
import os
import sys
import tempfile

CAMERAS = ("camera_1", "camera_2", "camera_3", "camera_4")
LIGHTING = ("day", "night")
TRIAL_COLUMNS = ("camera", "lighting", "expected", "detected")
WINDOW_COLUMNS = ("camera", "lighting", "hours", "false_alerts")
TRIAL_HEADER = ",".join(TRIAL_COLUMNS) + "\n"
WINDOW_HEADER = ",".join(WINDOW_COLUMNS) + "\n"

# The overall row sits under this label rather than under a group of its own, so
# a pooled figure can never be mistaken for a camera's.
ALL = "all"

EXIT_OK = 0
EXIT_BAD_INPUT = 2


class Refused(Exception):
    """A file this tool will not read, naming the line and column that stopped it."""


class Tally:
    """Four outcome counts, and the exposure a false-alert rate needs.

    Trials and windows are counted into the same shape but never the same
    object: a trial is one observation, a window is a stretch of unobserved
    time, and only the second has an hours denominator.
    """

    def __init__(self):
        self.tp = 0
        self.fp = 0
        self.fn = 0
        self.tn = 0
        self.hours = 0.0
        self.alerts = 0

    def add(self, expected, detected):
        if expected and detected:
            self.tp += 1
        elif detected:
            self.fp += 1
        elif expected:
            self.fn += 1
        else:
            self.tn += 1

    @property
    def trials(self):
        return self.tp + self.fp + self.fn + self.tn

    @property
    def measured(self):
        """True only once some hours have actually been watched. Zero hours is not
        a rate of zero alerts: it is no exposure, which is a different thing."""
        return self.hours > 0


def ratio(numerator, denominator):
    """(text, value) for a proportion, printed to three decimals or "undefined".

    Undefined is a real answer and is printed as such. Precision over no
    predicted positives and recall over no actual ones are 0/0, and printing
    0.000 for them would report a rule that never fired as flawless.
    """
    if denominator == 0:
        return "undefined", None
    value = numerator / denominator
    return f"{value:.3f}", value


def read_rows(path, columns):
    """[(line number, [cell, ...])] for every data row of one CSV, or Refused.

    The header must be the four names in order: the columns are positional, so a
    reordered or renamed header is a file this tool will not guess its way
    through. Line numbers are 1-based and count the header, so the number in a
    refusal is the number an editor sees. strict=True, so a stray quote is a
    refusal rather than a silently merged field.
    """
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle, strict=True))
    except FileNotFoundError:
        raise Refused(f"{path}: no such file")
    except IsADirectoryError:
        raise Refused(f"{path}: that is a directory, not a CSV file")
    except csv.Error as err:
        # csv.Error says what it choked on and nothing about this machine.
        raise Refused(f"{path}: not readable as CSV ({err})")
    except UnicodeDecodeError:
        raise Refused(f"{path}: not UTF-8 text")
    except OSError as err:
        # Type only: an OSError string can carry the path it was raised for.
        raise Refused(f"{path}: {type(err).__name__} reading it")

    wanted = ",".join(columns)
    if not rows:
        raise Refused(f"{path}: the file has no header line; header must be exactly {wanted}")
    header = [cell.strip() for cell in rows[0]]
    if header != list(columns):
        raise Refused(f"{path}:1: header must be exactly {wanted}, not {header!r}")

    out = []
    for number, raw in enumerate(rows[1:], start=2):
        # A genuinely blank line is not a row; a row of blanks is, and is refused
        # below as missing values rather than skipped.
        if not raw:
            continue
        if len(raw) != len(columns):
            raise Refused(
                f"{path}:{number}: {len(raw)} value(s), expected {len(columns)}: {raw!r}"
            )
        out.append((number, [cell.strip() for cell in raw]))
    return out


def camera_of(path, number, text):
    if text not in CAMERAS:
        raise Refused(f"{path}:{number}: camera {text!r} is not one of {', '.join(CAMERAS)}")
    return text


def lighting_of(path, number, text):
    if text not in LIGHTING:
        raise Refused(f"{path}:{number}: lighting {text!r} is not day or night")
    return text


def flag_of(path, number, column, text):
    """expected and detected are exactly "0" or "1".

    Anything else is refused rather than coerced: 2 is not a truthy answer, 01
    and 1.0 are two spellings that would let one row mean two different things,
    and yes/no is a different vocabulary that would silently score as a miss.
    """
    if text not in ("0", "1"):
        raise Refused(f"{path}:{number}: {column} {text!r} is not exactly 0 or 1")
    return text == "1"


def hours_of(path, number, text):
    try:
        value = float(text)
    except ValueError:
        raise Refused(f"{path}:{number}: hours {text!r} is not a number")
    if not math.isfinite(value):
        raise Refused(f"{path}:{number}: hours {text!r} is not finite")
    if value <= 0:
        raise Refused(
            f"{path}:{number}: hours {text!r} is not above 0, so there is no denominator"
        )
    return value


def count_of(path, number, text):
    """A non-negative whole number of false alerts.

    isdigit alone is not enough: it accepts other scripts' digits and superscripts,
    and int() then reads what looks like one thing as another. ASCII digits only.
    """
    if not text.isascii() or not text.isdigit():
        raise Refused(
            f"{path}:{number}: false_alerts {text!r} is not a non-negative whole number"
        )
    return int(text)


def load_trials(path):
    """{group: Tally}: one per camera and lighting, plus the overall under ALL.

    A camera with no night observations simply has no night group, and the report
    omits that row. A condition nobody tested is absent, never a row of zeros.
    """
    tally = {ALL: Tally()}
    for number, cells in read_rows(path, TRIAL_COLUMNS):
        camera = camera_of(path, number, cells[0])
        lighting = lighting_of(path, number, cells[1])
        expected = flag_of(path, number, "expected", cells[2])
        detected = flag_of(path, number, "detected", cells[3])
        group = tally.setdefault((camera, lighting), Tally())
        group.add(expected, detected)
        tally[ALL].add(expected, detected)
    return tally


def load_windows(path):
    """{group: Tally}: watched hours and counted false alerts, per group and overall.

    The running total is checked as it goes, because many small finite hours can
    add up to an infinity, and a rate printed against a total that has overflowed
    would be a number nobody observed.
    """
    tally = {ALL: Tally()}
    for number, cells in read_rows(path, WINDOW_COLUMNS):
        camera = camera_of(path, number, cells[0])
        lighting = lighting_of(path, number, cells[1])
        hours = hours_of(path, number, cells[2])
        alerts = count_of(path, number, cells[3])
        key = (camera, lighting)
        group = tally.setdefault(key, Tally())
        for target, label in ((group, key), (tally[ALL], ALL)):
            total = target.hours + hours
            if not math.isfinite(total):
                raise Refused(f"{path}:{number}: the watched hours for {label} do not add up "
                              "to a finite total, so no rate could be measured from them")
            target.hours = total
            target.alerts += alerts
    return tally


def label_of(key):
    if key == ALL:
        return "all cameras / both"
    return f"{key[0]} / {key[1]}"


def groups_in_order(tally):
    """The groups that exist, cameras then lighting, so two runs print alike."""
    return [
        key
        for camera in CAMERAS
        for lighting in LIGHTING
        if (key := (camera, lighting)) in tally
    ]


TRIAL_WIDTHS = (15, 7, 4, 4, 4, 4, 11, 8)
WINDOW_WIDTHS = (15, 9, 13, 17)


def table_row(cells, widths):
    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()


def trial_row(key, tally):
    precision, _ = ratio(tally.tp, tally.tp + tally.fp)
    recall, _ = ratio(tally.tp, tally.tp + tally.fn)
    return table_row(
        (
            label_of(key),
            str(tally.trials),
            str(tally.tp),
            str(tally.fp),
            str(tally.fn),
            str(tally.tn),
            precision,
            recall,
        ),
        TRIAL_WIDTHS,
    )


def window_row(key, tally):
    return table_row(
        (
            label_of(key),
            f"{tally.hours:g}",
            str(tally.alerts),
            "unmeasured" if not tally.measured else f"{tally.alerts / tally.hours:.3f}",
        ),
        WINDOW_WIDTHS,
    )


def report(trials_path, trials, windows_path, windows):
    lines = [
        "Detection accuracy for ONE declared rule, from observer-filled CSVs.",
        "",
        "Scoring scope: name the rule, model and config in force in your run notes,",
        "and score a separate run for each. Two rules scored together are one",
        "meaningless number. Precision and recall below are over staged",
        "observations of the scenes somebody watched: they are not deployment",
        "precision, and no figure here is a claim about an unobserved scene.",
    ]
    if trials_path is None:
        lines += [
            "",
            "trials    no --trials file: every trial count, precision and recall is",
            "          unmeasured.",
        ]
    else:
        lines += ["", f"trials    {trials_path}", ""]
        lines.append(
            table_row(
                ("camera/lighting", "trials", "TP", "FP", "FN", "TN", "precision", "recall"),
                TRIAL_WIDTHS,
            )
        )
        for key in groups_in_order(trials):
            lines.append(trial_row(key, trials[key]))
        lines.append(trial_row(ALL, trials[ALL]))

    if windows_path is None:
        lines += [
            "",
            "windows   no --windows file: false alerts per camera-hour is unmeasured,",
            "          which is not a rate of zero.",
        ]
    else:
        lines += ["", f"windows   {windows_path}", ""]
        lines.append(
            table_row(
                ("camera/lighting", "hours", "false alerts", "per camera-hour"),
                WINDOW_WIDTHS,
            )
        )
        for key in groups_in_order(windows):
            lines.append(window_row(key, windows[key]))
        lines.append(window_row(ALL, windows[ALL]))

    lines += [
        "",
        "A group with no rows was not observed and is absent, not zero. A false",
        "alert is counted against a clock by an observer and is never derived from",
        "the trials file: an empty stage is indistinguishable from a missed",
        "object, so the recall here only covers what somebody stood there and saw.",
    ]
    return "\n".join(lines)


# The disposable inputs the self-check scores. Every one of these is fake and
# none of it is a measurement; they exist to make the arithmetic and the
# refusals observable, and nothing in them describes any camera.
CHECK_TRIALS = TRIAL_HEADER + "camera_1,day,1,1\ncamera_1,day,1,0\ncamera_1,day,0,1\ncamera_1,day,0,0\n"
CHECK_WINDOWS = WINDOW_HEADER + "camera_1,day,2,1\n"
CHECK_QUIET = TRIAL_HEADER + "camera_4,night,0,0\ncamera_4,night,0,0\n"
# (what is being refused, which option reads it, its bytes, the words that must appear)
CHECK_REFUSED = (
    ("an unknown camera", "--trials", TRIAL_HEADER + "camera_5,day,1,1\n", "camera_5"),
    ("an unknown lighting", "--trials", TRIAL_HEADER + "camera_1,dusk,1,1\n", "not day or night"),
    ("a detected of 2", "--trials", TRIAL_HEADER + "camera_1,day,1,2\n", "not exactly 0 or 1"),
    ("a detected of 1.0", "--trials", TRIAL_HEADER + "camera_1,day,1,1.0\n", "not exactly 0 or 1"),
    ("a blank detected", "--trials", TRIAL_HEADER + "camera_1,day,1,\n", "not exactly 0 or 1"),
    ("a short row", "--trials", TRIAL_HEADER + "camera_1,day,1\n", "3 value(s), expected 4"),
    ("a renamed header", "--trials", "camera,light,expected,detected\ncamera_1,day,1,1\n",
     "header must be exactly"),
    ("a file with no header", "--trials", "", "header must be exactly"),
    ("zero hours", "--windows", WINDOW_HEADER + "camera_1,day,0,1\n", "is not above 0"),
    ("infinite hours", "--windows", WINDOW_HEADER + "camera_1,day,inf,1\n", "is not finite"),
    ("word hours", "--windows", WINDOW_HEADER + "camera_1,day,two,1\n", "is not a number"),
    ("negative false alerts", "--windows", WINDOW_HEADER + "camera_1,day,1,-1\n",
     "non-negative whole number"),
    ("fractional false alerts", "--windows", WINDOW_HEADER + "camera_1,day,1,1.5\n",
     "non-negative whole number"),
    ("hours that overflow", "--windows",
     WINDOW_HEADER + "camera_1,day,1e308,1\n" + "camera_1,day,1e308,1\n",
     "do not add up to a finite total"),
    ("an unbalanced quote", "--trials", TRIAL_HEADER + 'camera_1,day,"1,1\n', "not readable as CSV"),
    ("a missing file", "--trials", None, "no such file"),
)


def write_csv(folder, name, body):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    return path


def run_cli(argv):
    """(exit code, everything the CLI printed): the tool's own entry point, with a
    disposable file and nothing else in reach."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = main(argv)
    return code, buffer.getvalue()


def self_check():
    """One disposable check over the real parsers: the counted outcomes, the
    undefined cases, and every shape of bad number or bad CSV that must be
    refused before a single figure is printed."""
    with tempfile.TemporaryDirectory(prefix="evaluate-self-check-") as folder:
        counted = load_trials(write_csv(folder, "trials.csv", CHECK_TRIALS))
        group = counted[("camera_1", "day")]
        assert (group.tp, group.fp, group.fn, group.tn) == (1, 1, 1, 1), \
            f"one of each outcome was not counted as one: {vars(group)}"
        assert group.trials == 4 and counted[ALL].trials == 4, \
            f"four trials read as {group.trials} in the group and {counted[ALL].trials} overall"
        assert ratio(group.tp, group.tp + group.fp)[0] == "0.500", \
            "one true positive of two predicted positives is not 0.500 precision"
        assert ratio(group.tp, group.tp + group.fn)[0] == "0.500", \
            "one true positive of two actual positives is not 0.500 recall"
        print("ok  one true positive, one false positive, one false negative and one "
              "true negative counted separately, precision and recall both 0.500")

        quiet = load_trials(write_csv(folder, "quiet.csv", CHECK_QUIET))[("camera_4", "night")]
        assert ratio(quiet.tp, quiet.tp + quiet.fp)[0] == "undefined", \
            "precision over no predicted positives was not reported as undefined"
        assert ratio(quiet.tp, quiet.tp + quiet.fn)[0] == "undefined", \
            "recall over no actual positives was not reported as undefined"
        print("ok  a quiet camera reads as two true negatives with precision and recall "
              "undefined, not as 0.000")

        exposure = load_windows(write_csv(folder, "windows.csv", CHECK_WINDOWS))
        assert exposure[("camera_1", "day")].hours == 2.0, \
            f"two watched hours read as {exposure[('camera_1', 'day')].hours}"
        assert window_row(ALL, exposure[ALL]).endswith("0.500"), \
            f"one alert over two hours is not 0.500 per camera-hour: {window_row(ALL, exposure[ALL])!r}"
        assert window_row(ALL, Tally()).endswith("unmeasured"), \
            "no watched hours was reported as a rate instead of unmeasured"
        print("ok  one false alert over two watched hours is 0.500 per camera-hour, and no "
              "watched hours at all is unmeasured rather than zero")

        refused = 0
        for index, (name, option, body, expected) in enumerate(CHECK_REFUSED):
            path = os.path.join(folder, "missing.csv") if body is None \
                else write_csv(folder, f"bad{index}.csv", body)
            code, output = run_cli([option, path])
            assert code == EXIT_BAD_INPUT, f"{name} exited {code} instead of refusing"
            assert expected in output, f"{name} was refused without saying why: {output!r}"
            assert "precision" not in output and "camera-hour" not in output, \
                f"{name} still printed a counted outcome:\n{output}"
            refused += 1
        print(f"ok  {refused} bad camera, lighting, flag, row width, header, quote, hours, "
              f"overflow and count values refused with exit 2, none printing a figure")
    print("\nPASS  every assertion held")
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score staged detection trials and measured false alerts, from CSVs only"
    )
    parser.add_argument(
        "--trials", metavar="CSV",
        help=f"observed trials, columns {','.join(TRIAL_COLUMNS)}",
    )
    parser.add_argument(
        "--windows", metavar="CSV",
        help=f"watched empty windows, columns {','.join(WINDOW_COLUMNS)}",
    )
    parser.add_argument(
        "--self-check", action="store_true",
        help="score disposable files of this tool's own and exit",
    )
    args = parser.parse_args(argv)

    if args.self_check:
        if args.trials is not None or args.windows is not None:
            parser.error("--self-check scores disposable files of its own; drop --trials and --windows")
        try:
            return self_check()
        except (Refused, AssertionError) as err:
            print(f"evaluate: FAIL  {err}", file=sys.stderr)
            return 1

    if args.trials is None and args.windows is None:
        parser.error(
            "give --trials, --windows or both; neither was given, so there is nothing to "
            "score. --self-check runs the check with no file of its own"
        )

    try:
        trials = load_trials(args.trials) if args.trials is not None else None
        windows = load_windows(args.windows) if args.windows is not None else None
    except Refused as err:
        print(f"evaluate: refused: {err}", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(report(args.trials, trials, args.windows, windows))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())