import re
import sys
from pathlib import Path

def bump_version(version_str: str) -> str:
    parts = version_str.split('.')
    # Increment the last (patch) version number
    parts[-1] = str(int(parts[-1]) + 1)
    return '.'.join(parts)

def update_file(file_path: str, pattern: str, replacement: str) -> None:
    path = Path(file_path)
    if not path.exists():
        print(f"Error: {file_path} not found.")
        sys.exit(1)
        
    content = path.read_text()
    new_content, count = re.subn(pattern, replacement, content, count=1)
    
    if count == 0:
        print(f"Error: Could not find version pattern in {file_path}.")
        sys.exit(1)
        
    path.write_text(new_content)

def main() -> None:
    pyproject_path = Path('pyproject.toml')
    if not pyproject_path.exists():
        print("Error: pyproject.toml not found.")
        sys.exit(1)
        
    content = pyproject_path.read_text()
    match = re.search(r'version\s*=\s*"([^"]+)"', content)
    if not match:
        print("Error: Version string not found in pyproject.toml")
        sys.exit(1)
        
    current_version = match.group(1)
    new_version = bump_version(current_version)
    
    # Update pyproject.toml
    update_file(
        'pyproject.toml', 
        r'(version\s*=\s*)"([^"]+)"', 
        rf'\g<1>"{new_version}"'
    )
    
    # Update gitpulse/utils.py
    update_file(
        'gitpulse/utils.py', 
        r'(__version__\s*=\s*)"([^"]+)"', 
        rf'\g<1>"{new_version}"'
    )
    
    # Output the new version so GitHub Actions can capture it
    print(new_version)

if __name__ == '__main__':
    main()
