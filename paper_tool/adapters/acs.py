from .linked import LinkedPublisherAdapter, unique_links


ACS_FALLBACK = {
    "acscatal": "ACS Catalysis",
    "acsomega": "ACS Omega",
    "orglett": "Organic Letters",
    "joc": "The Journal of Organic Chemistry",
    "jacs": "Journal of the American Chemical Society",
}


def merge_si_links(*groups):
    return unique_links([item for group in groups for item in group])


class ACSAdapter(LinkedPublisherAdapter):
    key = "ACS"
    publisher_name = "American Chemical Society"
    fallback_journals = ACS_FALLBACK

    @classmethod
    def matches_doi(cls, doi):
        return doi.lower().startswith("10.1021/")
