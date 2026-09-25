from job_hunter.deduplicate import clean_url, identity_keys


def test_tracking_and_source_url_normalization():
    assert clean_url("https://www.linkedin.com/jobs/view/junior-qa-123?trk=search") == "https://www.linkedin.com/jobs/view/123"
    assert clean_url("https://example.com/job?jk=42&utm_source=email&gclid=abc") == "https://example.com/job?jk=42"
    assert clean_url("javascript:alert(1)") is None
    assert clean_url("https://user:secret@example.com/") is None


def test_cross_source_natural_identity(make_job):
    first = make_job()
    second = make_job(site="indeed", id="in-456", job_url="https://indeed.com/viewjob?jk=456", location="San Jose, San Jose, CR")
    assert first.id == second.id
    assert set(identity_keys(first)) & set(identity_keys(second))


def test_different_locations_are_distinct(make_job):
    first = make_job()
    second = make_job(id="li-124", job_url="https://www.linkedin.com/jobs/view/124", location="Heredia, Costa Rica")
    assert not set(identity_keys(first)) & set(identity_keys(second))


def test_unknown_companies_not_merged_by_title(make_job):
    first = make_job(company=None)
    second = make_job(company=None, id="li-456", job_url="https://example.com/456")
    assert not set(identity_keys(first)) & set(identity_keys(second))


def test_real_board_company_domain_and_province_variation(make_job):
    first = make_job(company="360training", location="Heredia, Heredia, Costa Rica")
    second = make_job(company="360training.com", location="Heredia, H, CR", site="indeed", id="in-42", job_url="https://indeed.com/42")
    assert first.id == second.id
