#!/usr/bin/env python3
"""Small regression checks for release naming rules."""

from release_manager import display_version, normalize_version


def main() -> None:
    assert normalize_version("v0.6.0") == "0.6.0"
    assert normalize_version("12.3.4") == "12.3.4"
    assert display_version("0.6.0", "beta") == "v0.6.0 beta"
    try:
        normalize_version("0.6 beta")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid semantic version was accepted")
    print("release manager: naming checks passed")


if __name__ == "__main__":
    main()
