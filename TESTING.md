# Oak Tree Gather smoke tests

Run the local smoke suite from the repository root after activating the virtual environment:

```powershell
python -m py_compile bot.py
python -m unittest discover -s tests -v
```

The suite checks that the major Discord interaction classes still exist and instantiate, the Location Set/Vote modals are present, the event setup view exposes its core controls, the Ping modal includes role/@here/@everyone choices, the setup preview renders those ping choices, and the SQLite migration/copy path preserves the new ping mode.

The included `.github/workflows/smoke-tests.yml` runs the same checks automatically on every push to `main` and on pull requests. The workflow uses a dummy Discord token and never connects the bot to Discord because `bot.run()` is protected by an `if __name__ == "__main__"` guard.

These are smoke tests, not end-to-end Discord API tests. A successful run catches missing classes, import/syntax failures, common UI construction regressions, and selected database migration/copy errors before Railway deployment.
