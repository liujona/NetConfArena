import re
from .device_profile import PROMPT_RE

def remove_redundant_prompt(text):
    while True:
        matches = list(re.finditer(PROMPT_RE, text, re.MULTILINE))
        if matches:
            last_match = matches[-1]
            text = text[:last_match.start()] + text[last_match.end():]
        else:
            break
    return text

def extract_prompt(text):
    matches = list(re.finditer(PROMPT_RE, text, re.MULTILINE))
    if not matches:
        return ""
    return text[matches[-1].start():matches[-1].end()]

def clean_configuration(config_text):
    # Remove the leading "Current configuration" line.
    config_text = re.sub(r"^.*?Current configuration : \d+ bytes\r?\n", "", config_text, flags=re.DOTALL)
    # Remove truncated content after the final "end" marker.
    config_text = re.sub(r'(\bend\s*)[\r\n]+([^!\s]+(?:\s*[^!\s]*)*)\s*$', r'\1', config_text)
    return config_text

def remove_banner_sections(config):
    lines = config.split("\n")
    result = []
    inside_banner = False

    for line in lines:
        if line.startswith("banner"):
            inside_banner = True
        elif inside_banner and line.startswith("!"):
            inside_banner = False
            continue  # Skip separators so `!` does not appear in the result.

        if not inside_banner:
            result.append(line)

    return "\n".join(result)

def clean_command_output(command, command_output):
    command = re.escape(command)
    cleaned_output = re.sub(fr"^.*?{command}\r\n", "", command_output, flags=re.DOTALL)
    return cleaned_output


def ensure_start_end(lst, start_value, end_value):
    lst = list(lst)
    if not lst:
        return [start_value, end_value]
    if lst[0] != start_value:
        lst.insert(0, start_value)
    if lst[-1] != end_value:
        lst.append(end_value)
    return lst
