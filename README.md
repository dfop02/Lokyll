# Lokyll
Lokyll allows you to localize your project to other langs, tricks your Liquid tags into staying safe during translation.

### Dependencies
- Python > 3.8 and Python < 3.12

### Setup
1. run `install_translator.py` by:
```bash
python install.py`
```
or
```bash
chmod +x install.py
./install.py
```

This will install all dependencies and make sure translator will works, by default it translate from `en` to `pt`.
If you want translate to another language, please edit it on `install.py`:
```python
# ---------------- Configuration ----------------
FROM_LANG = "en"   # Source language
TO_LANG = "pt"     # Target language
# ------------------------------------------------
```
You can find the supported langs [here](https://github.com/argosopentech/argos-translate?tab=readme-ov-file#supported-languages).

2. run `lokyll.py`

```bash
# From local path source
python lokyll.py --src test --dest test-pt --from-lang en --to-lang pt --include-markdown --translate-js

# From git repo source
python lokyll.py --repo-url https://github.com/crystal-lang/crystal-website --dest crystal-website-pt --from-lang en --to-lang pt --include-markdown --translate-js
```

### Authors

* [Diogo Fernandes](https://github.com/dfop02)

### License

This project is licensed under MIT - see the [LICENSE](LICENSE) file for details
