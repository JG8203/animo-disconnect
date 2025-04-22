from fastapi import FastAPI, Query, HTTPException
from typing import List, Dict, Any
from datetime import datetime, timezone
import uvicorn

app = FastAPI(title="Mock DLSU Scraper", version="1.0")

MOCK_DATA: List[Dict[str, Any]] = [
    {
        "classNbr": 2523,
        "course": "ACYITM2",
        "section": "K32",
        "enrlCap": 30,
        "remarks": "Open",
        "instructor": "Staff",
        "meetings": [
            {"day": "Mo", "time": "7:30AM - 10:45AM", "room": "VL306"},
            {"day": "Th", "time": "7:30AM - 10:45AM", "room": "TBA"},
        ],
    },
    {
        "classNbr": 101,
        "course": "CSOPESY",
        "section": "S14",
        "enrlCap": 30,
        "remarks": "Open",
        "instructor": "Staff",
        "meetings": [
            {"day": "Tu", "time": "9:15AM - 10:45AM", "room": "TBA"},
            {"day": "Fr", "time": "11:00AM - 12:30PM", "room": "GK201"},
        ],
    },
    {
        "classNbr": 656,
        "course": "GETEAMS",
        "section": "YY11",
        "enrlCap": 30,
        "remarks": "Open",
        "instructor": "Staff",
        "meetings": [
            {"day": "We", "time": "8:00AM - 10:00AM", "room": "TBA"},
        ],
    },
    {
        "classNbr": 541,
        "course": "LCFILIB",
        "section": "Y06",
        "enrlCap": 30,
        "remarks": "Open",
        "instructor": "Staff",
        "meetings": [
            {"day": "Mo", "time": "4:15PM - 5:45PM", "room": "TBA"},
            {"day": "Th", "time": "4:15PM - 5:45PM", "room": "TBA"},
        ],
    },
    {
        "classNbr": 4091,
        "course": "LCLSTRI",
        "section": "Y07",
        "enrlCap": 30,
        "remarks": "Open",
        "instructor": "Staff",
        "meetings": [
            {"day": "We", "time": "10:00AM - 12:00PM", "room": "TBA"},
        ],
    },
    {
        "classNbr": 3348,
        "course": "STCLOUD",
        "section": "S13",
        "enrlCap": 30,
        "remarks": "Open",
        "instructor": "Staff",
        "meetings": [
            {"day": "Mo", "time": "12:45PM - 2:15PM", "room": "TBA"},
            {"day": "Th", "time": "11:00AM - 12:30PM", "room": "GK304B - Computer Lab"},
        ],
    },
]

_STATE: Dict[int, bool] = {item["classNbr"]: False for item in MOCK_DATA}


@app.get("/scrape", response_model=List[Dict[str, Any]])
async def mock_scrape(
    course: str = Query(..., description="Course code, e.g. ACYITM2"),
    id_no: str = Query(..., description="Student ID, ignored in mock"),
) -> List[Dict[str, Any]]:
    """
    Return the mock offerings for `course`.  Each section's 'enrolled'
    toggles on every request: either full (==enrlCap) or one slot open.
    """
    course = course.upper()
    # filter matching subjects
    data = [item for item in MOCK_DATA if item["course"] == course]
    if not data:
        raise HTTPException(status_code=404, detail=f"No mock data for {course}")

    result: List[Dict[str, Any]] = []
    for item in data:
        was_full = _STATE[item["classNbr"]]
        _STATE[item["classNbr"]] = not was_full

        enrolled = item["enrlCap"] if was_full else (item["enrlCap"] - 1)
        section = item.copy()
        section["enrolled"] = enrolled
        section["timestamp"] = datetime.now(timezone.utc).isoformat()
        result.append(section)

    return result


if __name__ == "__main__":
    uvicorn.run("mock_server:app", host="0.0.0.0", port=8000, reload=True)
