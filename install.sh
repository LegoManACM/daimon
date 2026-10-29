#!/bin/sh
# Installs Daimon on macOS or Linux:
#   curl -fsSL https://github.com/LegoManACM/daimon/releases/latest/download/install.sh | sh
# DAIMON_VERSION=X.Y.Z picks a version (default: the latest release), DAIMON_NO_SETUP=1 skips `daimon setup`,
# DAIMON_DRY_RUN=1 prints what it would run instead of running it.
set -eu

REPO="LegoManACM/daimon"

say() { printf '%s\n' "$*"; }
fail() { printf 'daimon install: %s\n' "$*" >&2; exit 1; }

run() {
    if [ -n "${DAIMON_DRY_RUN:-}" ]; then
        say "+ $*"
    else
        "$@"
    fi
}

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        fail "this needs curl or wget."
    fi
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return
    fi
    for dir in "${XDG_BIN_HOME:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uv" ]; then
            say "$dir/uv"
            return
        fi
    done
}

latest_version() {
    fetch "https://api.github.com/repos/$REPO/releases/latest" \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/p' | head -n 1
}

main() {
    say ""
    say "  Daimon, a personal agent that works in the background"
    say "  ---------------------------------------------------"
    say ""
    [ -z "${DAIMON_DRY_RUN:-}" ] || say "Dry run: printing the commands instead of running them."

    uv=$(find_uv)
    if [ -z "$uv" ]; then
        say "Installing uv (it installs Python and Daimon for you)..."
        if [ -n "${DAIMON_DRY_RUN:-}" ]; then
            say "+ curl -LsSf https://astral.sh/uv/install.sh | sh"
            uv=uv
        else
            fetch https://astral.sh/uv/install.sh | sh
            uv=$(find_uv)
            [ -n "$uv" ] || fail "uv was installed but can't be found. Open a new terminal and run this again."
        fi
    fi

    version="${DAIMON_VERSION:-}"
    version="${version#v}"
    if [ -z "$version" ]; then
        version=$(latest_version) || true
        [ -n "$version" ] || fail "couldn't find the latest release. Set DAIMON_VERSION=X.Y.Z and try again."
    fi
    wheel="https://github.com/$REPO/releases/download/v$version/daimon-$version-py3-none-any.whl"

    # Keep the add-ons an earlier install has (the wizard adds them), so upgrading doesn't drop them.
    set -- tool install --python 3.12 --force "$wheel"
    py="$("$uv" tool dir 2>/dev/null)/daimon/bin/python"
    for m in daimon_canvas daimon_messages; do
        if [ -x "$py" ] && "$py" -c "import $m" >/dev/null 2>&1; then
            set -- "$@" --with "https://github.com/$REPO/releases/download/v$version/$m-$version-py3-none-any.whl"
        fi
    done

    say "Installing Daimon $version..."
    run "$uv" "$@"
    run "$uv" tool update-shell || true

    if [ -z "${DAIMON_DRY_RUN:-}" ]; then
        bin=$("$uv" tool dir --bin)
        PATH="$bin:$PATH"
        export PATH
    fi

    say ""
    say "Daimon $version is installed. If \`daimon\` isn't found later, open a new terminal."
    if [ -n "${DAIMON_NO_SETUP:-}" ]; then
        say "Run \`daimon setup\` when you're ready."
    elif [ -t 0 ]; then
        run daimon setup
    elif (: </dev/tty) 2>/dev/null; then
        if [ -n "${DAIMON_DRY_RUN:-}" ]; then
            say "+ daimon setup </dev/tty"
        else
            daimon setup </dev/tty
        fi
    else
        say "No terminal to ask questions in, so run \`daimon setup\` yourself to finish."
    fi
}

main "$@"
