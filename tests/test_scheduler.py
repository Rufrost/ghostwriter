from app.db import Store
from app.runtime import Runtime
from app.scheduler import Scheduler, symmetric_jitter_base
from app.service import ContentService


def test_symmetric_jitter_base():
    assert symmetric_jitter_base(9, 0, 10) == (8, 50)
    assert symmetric_jitter_base(0, 5, 10) == (23, 55)
    assert symmetric_jitter_base(19, 30, 0) == (19, 30)


def test_posting_jobs_use_symmetric_jitter(tmp_path):
    store = Store(tmp_path / "scheduler.db")
    store.init()
    rt = Runtime(store)
    rt.set("posting_times", "09:00,13:00,19:00")
    rt.set("posting_jitter_minutes", 10)
    scheduler = Scheduler(ContentService(store, rt), rt)

    scheduler._schedule_posting()
    jobs = [j for j in scheduler.scheduler.get_jobs() if j.id.startswith("posting-")]

    assert len(jobs) == 3
    assert all(job.trigger.jitter == 1200 for job in jobs)
    assert "hour='8'" in str(scheduler.scheduler.get_job("posting-0900").trigger)
    assert "minute='50'" in str(scheduler.scheduler.get_job("posting-0900").trigger)
    assert "3/день" in scheduler.describe()
    assert "±10 мин" in scheduler.describe()
    assert scheduler.scheduler.get_job("strategy") is None


async def test_posting_job_uses_user_queue_and_skips_empty_queue(tmp_path):
    store = Store(tmp_path / "queue-scheduler.db")
    store.init()
    rt = Runtime(store)
    rt.set("auto_approve", False)
    service = ContentService(store, rt)
    scheduler = Scheduler(service, rt)

    await scheduler._posting_job()
    assert store.recent_posts() == []

    post_id = service.queue_user_posts(["Готовый пост для расписания"])[0]
    await scheduler._posting_job()

    assert store.get_post(post_id)["status"] == "pending_approval"
    assert store.count_queued_posts() == 0
