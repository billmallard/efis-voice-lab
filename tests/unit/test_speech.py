from lab.agent.speech import normalize


def test_markdown_and_code_markup_is_removed():
    assert normalize("Press the **Units** button.") == "Press the Units button."
    assert normalize("Install the `qt` extra.") == "Install the qt extra."
    assert normalize("```bash\nmake test\n```") == "make test"
    assert normalize("## Setup\nRun it.") == "Setup. Run it."


def test_lists_become_sentences():
    assert normalize("Steps:\n- clone the repo\n- run make venv\n1. start it") == \
        "Steps: clone the repo. run make venv. start it"


def test_links_and_urls():
    assert normalize("See [the guide](https://example.com/x).") == "See the guide."
    assert normalize("Go to https://navdata.aerocommons.org.") == "Go to a link."


def test_units_and_symbols_are_said_as_words():
    assert normalize("About 120 GB on disk") == "About 120 gigabytes on disk"
    assert normalize("°C ⇄ °F") == "degrees Celsius and degrees Fahrenheit"
    assert normalize("29.92 inHg or 1013 hPa") == \
        "29.92 inches of mercury or 1013 hectopascals"
    assert normalize("~90 GB") == "about 90 gigabytes"


def test_content_is_not_rewritten():
    # Commands and identifiers survive: describing them is the prompt's job, and the
    # speakability judge still fails them.
    assert normalize("Run sudo python3 setup.py install") == "Run sudo python3 setup.py install"
    assert normalize("snake_case_name and a*b") == "snake_case_name and a*b"
