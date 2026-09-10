from .linked import LinkedPublisherAdapter


OPTICA_JOURNALS = {
    'oe': 'Optics Express',
    'ol': 'Optics Letters',
    'ao': 'Applied Optics',
    'josa': 'Journal of the Optical Society of America',
    'prj': 'Photonics Research',
    'photonics': 'Photonics Research',
}


class OpticaAdapter(LinkedPublisherAdapter):
    """opg.optica.org: a custom puzzle-font text captcha gates article pages.

    Like IOP's Radware wall it cannot be auto-solved; after the automatic
    attempts the adapter waits for the operator to type the captcha in the
    visible window, then retries with the cleared session.
    """

    key = 'OPTICA'
    publisher_name = 'Optica Publishing Group'
    fallback_journals = OPTICA_JOURNALS

    @classmethod
    def matches_doi(cls, doi):
        return doi.lower().startswith('10.1364/')

    async def prepare_article(self, ctx):
        url, issue = await super().prepare_article(ctx)
        if issue and ('challenge' in issue
                      or await self.page_challenge_state(ctx.tab)):
            if await self.wait_for_manual_challenge(ctx):
                ctx.navigation_diagnostics['manual_challenge_retried'] = True
                url = await self.navigate(ctx, cloudflare=False)
                ready = await self.wait_for_article_dom(
                    ctx.tab, timeout=ctx.settings.cloudflare_timeout_seconds,
                )
                issue = await self.access_issue(ctx.tab)
                if ready and not issue:
                    return url, None
        return url, issue
