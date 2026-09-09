from .linked import LinkedPublisherAdapter


RSC_FALLBACK = {
    'qo': 'Organic Chemistry Frontiers', 'ra': 'RSC Advances',
    'cc': 'Chemical Communications', 'dt': 'Dalton Transactions',
    'cp': 'Physical Chemistry Chemical Physics', 'ta': 'Journal of Materials Chemistry A',
    'tb': 'Journal of Materials Chemistry B', 'tc': 'Journal of Materials Chemistry C',
    'nj': 'New Journal of Chemistry',
}


class RSCAdapter(LinkedPublisherAdapter):
    key = "RSC"
    publisher_name = "Royal Society of Chemistry"
    fallback_journals = RSC_FALLBACK

    @classmethod
    def matches_doi(cls, doi):
        return doi.lower().startswith("10.1039/")
