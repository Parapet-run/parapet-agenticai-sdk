"""One adapter per framework. Each exposes:

    NAME          str   -- display name
    INTEGRATION   str   -- the line a developer actually writes
    available()   bool  -- is the framework importable here
    run(prompt, ran) -> None

`ran` is a dict the tools flip on entry, so "was it blocked" is decided by
whether the tool body executed -- not by trusting a log line.
"""
