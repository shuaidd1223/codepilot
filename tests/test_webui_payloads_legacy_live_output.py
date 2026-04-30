from __future__ import annotations

from textwrap import dedent

from codepilot.webapp.live_output_payloads import normalize_legacy_live_output_markdown


def test_normalize_legacy_live_output_splits_runtime_role_and_exec_sections():
    raw = dedent(
        """
        ## Intro

        ## Live Output

        ~~~text
        booting runtime line
        user
        请实现 A
        assistant
        正在分析
        exec
        exec "rg -n foo" in D:\\repo
         succeeded in 12ms:
        foo
        ~~~
        ## Footer
        done
        """
    ).lstrip()

    normalized = normalize_legacy_live_output_markdown(raw)

    assert normalized == dedent(
        """
        ## Intro

        ## Live Output

        ### Runtime

        ~~~text
        booting runtime line
        ~~~

        ### User

        请实现 A
        ### Assistant

        正在分析
        ### Exec

        ~~~text
        exec "rg -n foo" in D:\\repo
         succeeded in 12ms:
        foo
        ~~~

        ## Footer
        done
        """
    ).lstrip()


def test_normalize_legacy_live_output_keeps_non_text_fence_unchanged():
    raw = dedent(
        """
        ## Live Output

        ~~~bash
        user
        echo "hello"
        ~~~
        """
    ).lstrip()

    assert normalize_legacy_live_output_markdown(raw) == raw


def test_normalize_legacy_live_output_keeps_plain_text_block_unchanged():
    raw = dedent(
        """
        ## Live Output

        ~~~text
        booting
        still booting
        ~~~
        """
    ).lstrip()

    assert normalize_legacy_live_output_markdown(raw) == raw


def test_normalize_legacy_live_output_handles_missing_closing_fence():
    raw = dedent(
        """
        ## Live Output

        ~~~text
        user
        hi
        exec
        echo hi
        """
    ).lstrip()

    normalized = normalize_legacy_live_output_markdown(raw)

    assert normalized == dedent(
        """
        ## Live Output

        ### User

        hi
        ### Exec

        ~~~text
        echo hi
        ~~~
        """
    ).lstrip()

