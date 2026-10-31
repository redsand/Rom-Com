"""The command line at least loads: a syntax error in cli.py passed the whole suite once,
because no test imported it."""


def test_the_cli_imports_and_knows_its_commands():
    from romcom import cli
    assert callable(cli.main) and callable(cli.cmd_android) and callable(cli.cmd_card)
