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
