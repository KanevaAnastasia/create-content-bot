"""Required real identities for the editorial historical series."""
import re

# Search forms include Russian inflections; names are stable character identities.
PEOPLE = (
    ('Стивен Кинг', r'Стивен.{0,12}Кинг|Кэрри|Stephen King|^Кинг', r'Стивен.*Кинг|Stephen.*King'),
    ('Табита Кинг', r'Табит|Tabitha', r'Табита(?: Джейн)? Кинг|Tabitha(?: Jane)? King'),
    ('Майя Энджелоу', r'Энджелоу|Maya Angelou', r'Май[яи].*Энджелоу|Maya.*Angelou'),
    ('Джеймс Дайсон', r'Дайсон|James Dyson', r'Джеймс.*Дайсон|James.*Dyson'),
    ('Октавия Батлер', r'Батлер|Octavia Butler', r'Октав\w*.*Батлер|Octavia.*Butler'),
    ('Чарльз Дарвин', r'Дарвин|Charles Darwin', r'Чарльз.*Дарвин|Charles.*Darwin'),
    ('Леонардо да Винчи', r'Леонардо|Leonardo da Vinci', r'Леонардо.*Винчи|Leonardo.*Vinci'),
    ('Винсент Ван Гог', r'Ван Гог|Vincent van Gogh', r'Винсент.*[Вв]ан.*Гог|Vincent.*Gogh'),
    ('Тони Моррисон', r'Моррисон|Toni Morrison', r'Тони.*Моррисон|Toni.*Morrison'),
    ('Харуки Мураками', r'Мураками|Haruki Murakami', r'Харуки.*Мураками|Haruki.*Murakami'),
    ('Симона Байлз', r'Байлз|Simone Biles', r'Симон\w*.*Байлз|Simone.*Biles'),
)


def required_people(source):
    return [(name, pattern) for name, search, pattern in PEOPLE if re.search(search, source, re.I)]


def validate_story_people(source, characters, scenes):
    used = {key for scene in scenes for key in scene.get('characters', [])}
    names = [str(c.get('display_name') or '') for c in characters if c.get('id') in used]
    required = required_people(source)
    missing = [name for name, pattern in required if not any(re.search(pattern, n, re.I) for n in names)]
    if missing:
        raise ValueError('В истории должны действовать сами реальные люди: ' + ', '.join(missing) + '. Укажите их полные имена в библии и составе сцен.')
    if not used:
        raise ValueError('Историческая история должна показывать своего героя, а не только предметы.')
