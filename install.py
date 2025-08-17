#!/usr/bin/env python3
import os
import sys
import subprocess
import tempfile
import urllib.request
from pathlib import Path

# ---------------- Configuration ----------------
FROM_LANG = "en"   # Source language
TO_LANG = "pt"     # Target language
# ------------------------------------------------

if sys.version_info < (3, 8) and sys.version_info < (3, 12):
    sys.exit("❌ Please use Python 3.8 > and Python < 3.12.")

# Step 1: Create virtual environment
venv_dir = Path(".translator_env")
if not venv_dir.exists():
    print("📦 Creating virtual environment...")
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
else:
    print("ℹ️ Virtual environment already exists.")

# Step 2: Install dependencies
pip_exe = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "pip"
python_exe = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"

print("📦 Installing dependencies...")
subprocess.run([str(pip_exe), "install", "--quiet", "--upgrade", "pip"], check=True)
subprocess.run([str(pip_exe), "install", "--quiet", "beautifulsoup4", "lxml", "pyyaml", "argostranslate"], check=True)

# Step 3: Install Argos Translate language pack
print(f"🌍 Installing Argos Translate language pack: {FROM_LANG} → {TO_LANG}...")
install_code = f"""
import argostranslate.package
import argostranslate.translate

from_code = "{FROM_LANG}"
to_code = "{TO_LANG}"

# Download and install Argos Translate package
argostranslate.package.update_package_index()
available_packages = argostranslate.package.get_available_packages()
package_to_install = next(
    filter(
        lambda x: x.from_code == from_code and x.to_code == to_code, available_packages
    )
)
argostranslate.package.install_from_path(package_to_install.download())

print("✅ Installed", "{FROM_LANG}-{TO_LANG}")
"""
subprocess.run([str(python_exe), "-c", install_code], check=True)

print("\n🎉 Setup complete!")
print(f"💡 Activate environment with: source {venv_dir}/bin/activate  (Linux/Mac)")
print(f"💡 Or: {venv_dir}\\Scripts\\activate  (Windows)")
print("Then run: python translate_site.py --help")
