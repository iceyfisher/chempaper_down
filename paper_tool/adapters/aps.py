from .linked import LinkedPublisherAdapter


APS_JOURNALS = {
    'prapplied': 'Physical Review Applied',
    'prl': 'Physical Review Letters',
    'prb': 'Physical Review B',
    'pra': 'Physical Review A',
    'prx': 'Physical Review X',
    'physrevresearch': 'Physical Review Research',
    'physrevapplied': 'Physical Review Applied',
}


class ApsAdapter(LinkedPublisherAdapter):
    """journals.aps.org: Cloudflare interstitial clears itself in a real
    browser within a few seconds; citation_pdf_url serves the PDF."""

    key = 'APS'
    publisher_name = 'American Physical Society'
    fallback_journals = APS_JOURNALS

    @classmethod
    def matches_doi(cls, doi):
        return doi.lower().startswith('10.1103/')
