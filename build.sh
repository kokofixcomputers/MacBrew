rm MacBrew.pyz
python -m zipapp . -o MacBrew.pyz -m "main:main"

# Build pyc
rm -rf __pycache__
rm merged.py
python -m python_files_merger main.py macbrew_core.py macbrew_constants.py macbrew_livecheck.py macbrew_metadata.py macbrew_network.py macbrew_utils.py macbrew_taps.py macbrew_packages.py macbrew_parsers.py -o merged.py
python scripts/fix_escape_sequences.py merged.py
pyminify merged.py > merged.min.py
python -c "import py_compile; py_compile.compile('merged.min.py', 'macbrew.pyc')"