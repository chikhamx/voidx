"""Shell command parsing and classification constants."""

READ_COMMANDS = {"cat", "head", "tail", "wc", "ls", "pwd", "echo", "printf"}
POWERSHELL_READ_COMMANDS = {
    "get-content", "gc", "cat", "type", "get-childitem", "gci", "dir", "ls",
    "write-output", "echo",
}
DYNAMIC_MARKERS = ("$", "`", "<(", ">(")
NESTED_INTERPRETERS = {
    "bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh", "python",
    "python3", "node", "ruby", "perl",
}
SHELL_OPERATOR_CHARS = {";", "|", "<", ">", "&", "\n", "\r"}

GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    "-C", "-c", "--config-env", "--git-dir", "--work-tree", "--namespace",
    "--exec-path", "--super-prefix",
})
GIT_GLOBAL_OPTIONS_OPTIONAL_VALUE = frozenset({"--exec-path"})
GIT_GLOBAL_OPTIONS = frozenset({
    "-h", "-v", "--help", "--version", "-p", "-P", "--paginate", "--no-pager",
    "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks",
    "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
    "--icase-pathspecs", "--bare",
})

GIT_UNSAFE_GLOBAL_OPTIONS = frozenset({
    "-c", "--config-env", "--exec-path",
})

GIT_READ_ONLY_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
    "ls-files", "ls-tree", "describe", "shortlog", "reflog", "cherry",
    "whatchanged", "grep", "cat-file", "name-rev", "for-each-ref",
})

# Only these options are accepted for a read-only subcommand.  Values are
# validated by the shell policy; unknown options are never treated as reads.
GIT_READ_ONLY_FLAGS: dict[str, frozenset[str]] = {
    "status": frozenset({
        "-s", "--short", "-b", "--branch", "--porcelain", "--ahead-behind",
        "--no-ahead-behind", "--show-stash", "--no-renames", "--renames",
        "--no-column",
    }),
    "log": frozenset({
        "--oneline", "--all", "--graph", "--stat", "--shortstat", "--numstat",
        "--patch", "-p", "--no-patch", "-s", "--name-only", "--name-status",
        "--follow", "--first-parent", "--merges", "--no-merges", "--reverse",
        "--topo-order", "--date-order", "--author-date-order", "--boundary",
        "--full-history", "--dense", "--sparse", "--no-walk", "--no-renames",
        "--renames", "--cherry-pick", "--left-right", "--cherry-mark",
        "--no-abbrev-commit", "--abbrev-commit", "--use-mailmap", "--mailmap",
        "--no-decorate", "--relative-date", "--reflog", "--no-notes",
    }),
    "diff": frozenset({
        "--cached", "--staged", "--stat", "--shortstat", "--numstat", "--patch",
        "-p", "--no-patch", "-s", "--name-only", "--name-status", "--check",
        "--summary", "--raw", "--patch-with-stat", "--no-renames", "--renames",
        "--minimal", "--patience", "--histogram", "--no-color", "--color-moved",
        "--no-color-moved", "--relative", "--no-ext-diff",
    }),
    "show": frozenset({
        "--oneline", "--stat", "--shortstat", "--numstat", "--patch", "-p",
        "--no-patch", "-s", "--name-only", "--name-status", "--raw", "--summary",
        "--no-renames", "--renames", "--no-color", "--relative",
    }),
    "blame": frozenset({
        "-e", "--show-email", "-w", "--ignore-rev", "--ignore-revs-file",
        "--show-stats", "--line-porcelain", "--porcelain", "--incremental",
        "--root", "--reverse",
    }),
    "rev-parse": frozenset({
        "--revs-only", "--no-revs", "--flags", "--no-flags", "--verify",
        "--quiet", "-q", "--short", "--sq-quote", "--not", "--symbolic",
        "--symbolic-full-name", "--abbrev-ref", "--disambiguate", "--all",
        "--branches", "--tags", "--remotes", "--glob", "--exclude",
        "--show-toplevel", "--show-prefix", "--show-cdup", "--show-superproject-working-tree",
        "--is-inside-git-dir", "--is-inside-work-tree", "--is-bare-repository",
        "--local-env-vars", "--path-format",
    }),
    "rev-list": frozenset({
        "--objects", "--objects-edge", "--all", "--stdin", "--topo-order",
        "--date-order", "--author-date-order", "--reverse", "--parents",
        "--children", "--timestamp", "--header", "--boundary", "--left-right",
        "--count", "--cherry-pick", "--cherry-mark", "--use-bitmap-index",
        "--bisect", "--bisect-vars", "--bisect-all", "--no-walk",
    }),
    "ls-files": frozenset({
        "-c", "--cached", "-d", "--deleted", "-m", "--modified", "-o",
        "--others", "-i", "--ignored", "-s", "--stage", "-u", "--unmerged",
        "-k", "--killed", "--directory", "--no-empty-directory", "-z",
        "--deduplicate", "--exclude-standard", "--full-name", "--recurse-submodules",
    }),
    "ls-tree": frozenset({
        "-d", "--full-tree", "-r", "-t", "--name-only", "--name-status", "--long",
        "-l", "-z", "--full-name", "--abbrev", "--object-only", "--format",
    }),
    "describe": frozenset({
        "--all", "--tags", "--contains", "--long", "--always", "--dirty",
        "--broken", "--exact-match", "--candidates", "--match", "--exclude",
        "--abbrev", "--first-parent", "--debug",
    }),
    "shortlog": frozenset({
        "-s", "--summary", "-n", "--numbered", "-e", "--email", "--group",
        "--committer", "--author", "--no-merges", "--all", "--not",
    }),
    "cherry": frozenset({"-v", "--verbose", "-r", "--right-only", "--abbrev"}),
    "whatchanged": frozenset({
        "--oneline", "--all", "--stat", "--shortstat", "--numstat", "--patch",
        "-p", "--no-patch", "-s", "--name-only", "--name-status", "--follow",
        "--first-parent", "--no-merges", "--reverse", "--no-renames", "--renames",
    }),
    "grep": frozenset({
        "-v", "--invert-match", "-n", "--line-number", "-h", "--no-name",
        "-H", "--full-name", "-l", "--files-with-matches", "-L", "--files-without-match",
        "-z", "--null", "-w", "--word-regexp", "-i", "--ignore-case", "-I",
        "--text", "-E", "--extended-regexp", "-F", "--fixed-strings", "-P",
        "--perl-regexp", "-a", "--textconv", "--break", "--heading", "--function-context",
    }),
    "cat-file": frozenset({
        "-t", "-s", "-e", "-p", "--filters", "--path", "--textconv", "--batch",
        "--batch-check", "--batch-all-objects", "--unordered", "--follow-symlinks",
        "--use-mailmap",
    }),
    "name-rev": frozenset({"--name-only", "--tags", "--no-undefined", "--always", "--stdin"}),
    "for-each-ref": frozenset({"--count", "--shell", "--python", "--tcl", "--perl", "--format", "--sort", "--points-at", "--merged", "--no-merged", "--contains", "--no-contains"}),
}

GIT_READ_ONLY_OPTIONS_WITH_VALUE: dict[str, frozenset[str]] = {
    "log": frozenset({"-n", "--max-count", "--skip", "--since", "--after", "--until", "--before", "--author", "--committer", "--grep", "--format", "--pretty", "--date", "--diff-merges", "--abbrev"}),
    "diff": frozenset({"-U", "--unified", "--diff-filter", "--submodule", "--word-diff", "--color", "--src-prefix", "--dst-prefix", "--line-prefix", "--inter-hunk-context", "--function-context"}),
    "show": frozenset({"--format", "--pretty", "--abbrev", "--diff-merges", "--color", "--src-prefix", "--dst-prefix"}),
    "blame": frozenset({"-L", "--ignore-rev", "--ignore-revs-file", "--contents", "--abbrev", "--line-porcelain"}),
    "rev-parse": frozenset({"--prefix", "--default", "--output-object-format", "--disambiguate"}),
    "rev-list": frozenset({"--max-count", "--max-age", "--min-age", "--since", "--after", "--until", "--before", "--grep", "--author", "--committer", "--remove-empty", "--diff-filter", "--max-parents", "--min-parents"}),
    "ls-files": frozenset({"--format", "--exclude", "--exclude-from", "--exclude-per-directory", "--exclude-standard"}),
    "ls-tree": frozenset({"--format", "--abbrev"}),
    "describe": frozenset({"--candidates", "--match", "--exclude", "--abbrev", "--dirty", "--broken"}),
    "shortlog": frozenset({"--group", "--format", "--committer", "--author"}),
    "cherry": frozenset({"--abbrev"}),
    "grep": frozenset({"-C", "--context", "-A", "--after-context", "-B", "--before-context", "-O", "--open-files-in-pager", "--max-count", "--threads", "--column"}),
    "cat-file": frozenset({"--batch-command", "--buffer", "--batch-all-objects"}),
    "name-rev": frozenset({"--name-ref", "--refs"}),
    "for-each-ref": frozenset({"--count", "--format", "--sort", "--points-at", "--merged", "--no-merged", "--contains", "--no-contains"}),
}

GIT_READ_ONLY_OPTIONS_WITH_OPTIONAL_VALUE: dict[str, frozenset[str]] = {
    "status": frozenset({"--porcelain", "--untracked-files", "-u", "--ignored", "--ignore-submodules", "--column", "--find-renames"}),
    "log": frozenset({"--decorate", "--color", "--abbrev-commit"}),
    "diff": frozenset({"--find-renames", "--find-copies", "--color-moved"}),
    "show": frozenset({"--find-renames", "--find-copies"}),
    "describe": frozenset({"--dirty", "--broken"}),
}

GIT_READ_ONLY_POSITIONAL_SUBCOMMANDS = frozenset({
    "log", "diff", "show", "blame", "rev-parse", "rev-list", "ls-files",
    "ls-tree", "describe", "shortlog", "reflog", "cherry", "whatchanged",
    "grep", "cat-file", "name-rev", "for-each-ref",
})

GIT_READ_ONLY_OUTPUT_FLAGS = frozenset({"-o", "--output"})
GIT_READ_ONLY_UNSAFE_OPTIONS = frozenset({
    "--ext-diff", "--textconv", "--filters", "--open-files-in-pager",
})

GIT_SAFE_ENV_NAMES = frozenset({
    "LANG", "LANGUAGE", "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LC_MESSAGES",
    "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "NO_COLOR", "TERM", "TZ",
})

GIT_REF_WRITE_FLAGS = frozenset({
    "-d", "-D", "-m", "-M", "--delete", "--move", "--force",
})

GIT_COMMON_OPTIONS_WITH_VALUE = frozenset({
    "--message",
    "--file",
    "--template",
    "--reedit-message",
    "--reuse-message",
    "--fixup",
    "--squash",
    "--author",
    "--committer",
    "--date",
    "--cleanup",
    "--strategy",
    "--strategy-option",
    "--encoding",
    "--grep",
    "--grep-reflog",
})

GIT_SUBCOMMAND_OPTIONS_WITH_VALUE: dict[str, frozenset[str]] = {
    **GIT_READ_ONLY_OPTIONS_WITH_VALUE,
    "commit": frozenset({
        "-m", "--message", "-F", "--file", "-t", "--template",
        "-c", "-C", "--reedit-message", "--reuse-message",
        "--fixup", "--squash", "--author", "--date", "--cleanup",
    }),
    "tag": frozenset({"-m", "--message", "-F", "--file", "-u", "--local-user"}),
    "merge": frozenset({"-m", "--message", "-F", "--file", "-s", "--strategy", "-X", "--strategy-option"}),
    "revert": frozenset({"-m", "--mainline", "-s", "--strategy", "-X", "--strategy-option"}),
    "cherry-pick": frozenset({"-s", "--strategy", "-X", "--strategy-option", "-m", "--mainline"}),
    "rebase": frozenset({"--onto", "-s", "--strategy", "-X", "--strategy-option"}),
    "stash": frozenset({"-m", "--message"}),
    "format-patch": frozenset({"-o", "--output-directory"}),
    "checkout": frozenset({"-b", "-B", "-t", "--track"}),
    "switch": frozenset({"-c", "-C", "-t", "--track"}),
    "notes": frozenset({"-m", "--message", "-F", "--file", "-c", "-C", "--reedit-message", "--reuse-message"}),
    "clone": frozenset({"-b", "--branch", "--reference", "--reference-if-able", "--template", "--origin", "-o", "-u", "--upload-pack"}),
}
