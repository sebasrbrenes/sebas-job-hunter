import pytest

from job_hunter.prefilter import prefilter


@pytest.mark.parametrize("requirement", ["1-2 years experience required", "2-3 years experience required",
    "Bachelor's degree required", "Bachelor's degree preferred", "Selenium, Playwright, SQL, Jira and API testing required",
    "Collaborate with senior engineers", "Five senior stakeholders will review results"])
def test_feasible_gaps_survive(requirement, make_job, preferences):
    job = prefilter(make_job(description=requirement), preferences)
    assert job.prefilter_score >= preferences.prefilter.minimum_score
    assert not job.prefilter_excluded
    assert not any("Senior/management" in reason for reason in job.prefilter_reasons)


def test_senior_title_and_required_years_penalized(make_job, preferences):
    junior = prefilter(make_job(), preferences)
    senior = prefilter(make_job(title="Senior QA Engineer", description="Minimum 7 years experience required."), preferences)
    assert senior.prefilter_score < junior.prefilter_score - 30


def test_preferred_years_and_company_age_not_required(make_job, preferences):
    job = prefilter(make_job(description="5+ years preferred. Company has 20 years of experience."), preferences)
    assert not any("years of experience appears required" in r for r in job.prefilter_reasons)


def test_lead_needs_context(make_job, preferences):
    a = prefilter(make_job(title="QA Lead", description="Python testing."), preferences)
    b = prefilter(make_job(title="QA Lead", description="Leadership experience required managing a team."), preferences)
    assert b.prefilter_score < a.prefilter_score


def test_internship_toggle(make_job, preferences):
    job = make_job(title="QA Intern", job_type="internship")
    assert prefilter(job, preferences).prefilter_excluded
    preferences.prefilter.allow_internships = True
    assert not prefilter(job, preferences).prefilter_excluded


def test_unpaid_context(make_job, preferences):
    assert not prefilter(make_job(description="Includes unpaid leave options."), preferences).prefilter_excluded
    assert prefilter(make_job(description="This position is unpaid."), preferences).prefilter_excluded


def test_unrelated_title_ranks_lower(make_job, preferences):
    good = prefilter(make_job(), preferences)
    bad = prefilter(make_job(title="Mechanical Quality Engineer"), preferences)
    assert bad.prefilter_score < good.prefilter_score - 25


def test_escaped_markdown_required_years(make_job, preferences):
    job = prefilter(make_job(description="Minimum 5\\+ years experience required."), preferences)
    assert any("5+ years" in reason for reason in job.prefilter_reasons)


def test_upper_end_of_range_does_not_become_minimum(make_job, preferences):
    job = prefilter(make_job(description="3-5 years of experience required."), preferences)
    assert not any("5+ years" in reason for reason in job.prefilter_reasons)
