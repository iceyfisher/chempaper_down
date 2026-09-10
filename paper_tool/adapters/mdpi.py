from .linked import LinkedPublisherAdapter


MDPI_JOURNALS = {
    'mi': 'Micromachines',
    'electronics': 'Electronics',
    'sensors': 'Sensors',
    'materials': 'Materials',
    'nanomaterials': 'Nanomaterials',
    'crystals': 'Crystals',
    'coatings': 'Coatings',
    'metals': 'Metals',
    'polymers': 'Polymers',
    'molecules': 'Molecules',
    'ijms': 'International Journal of Molecular Sciences',
    'appliedsci': 'Applied Sciences',
    'chemengineering': 'ChemEngineering',
}


class MdpiAdapter(LinkedPublisherAdapter):
    """mdpi.com open-access articles: citation_pdf_url plus /s<n> SI files."""

    key = 'MDPI'
    publisher_name = 'MDPI'
    fallback_journals = MDPI_JOURNALS

    @classmethod
    def matches_doi(cls, doi):
        return doi.lower().startswith('10.3390/')
