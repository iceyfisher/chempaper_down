from .linked import LinkedPublisherAdapter


IOP_JOURNALS = {
    'jap': 'Japanese Journal of Applied Physics',
    'jjap': 'Japanese Journal of Applied Physics',
    'apexp': 'Applied Physics Express',
    '1882-0786': 'Applied Physics Express',
    'd': 'Journal of Physics D: Applied Physics',
    'cm': 'Journal of Physics: Condensed Matter',
    'nano': 'Nanotechnology',
    'sst': 'Semiconductor Science and Technology',
}


class IopAdapter(LinkedPublisherAdapter):
    """iopscience.iop.org, including JJAP (10.7567).

    IOP fronts the site with Radware Bot Manager (hCaptcha). The Pydoll
    Cloudflare helper cannot auto-solve that, so once the automatic attempts
    fail the adapter waits for the operator to clear the captcha in the
    visible browser window; the persistent profile keeps the clearance for
    the remaining DOIs of the batch.
    """

    key = 'IOP'
    publisher_name = 'IOP Publishing'
    fallback_journals = IOP_JOURNALS

    @classmethod
    def matches_doi(cls, doi):
        return doi.lower().startswith(('10.1088/', '10.7567/'))

    async def prepare_article(self, ctx):
        url, issue = await super().prepare_article(ctx)
        if issue and ('challenge' in issue
                      or await self.page_challenge_state(ctx.tab)):
            # The hCaptcha wall needs a human. Wait outside the bounded
            # automatic attempts, then retry with the cleared session.
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
