import glob
import subprocess
import zipfile

subprocess.run(["pip", "install", "-q", "build"], check=True, capture_output=True)
r = subprocess.run(["python", "-m", "build", "--wheel", "--outdir", "/tmp/w"], capture_output=True, text=True)
print("build rc", r.returncode)
print(r.stdout[-1500:])
print(r.stderr[-1500:])
for w in glob.glob("/tmp/w/*.whl"):
    names = [n for n in zipfile.ZipFile(w).namelist() if "console" in n]
    print(w)
    for n in names:
        print("  ", n)
