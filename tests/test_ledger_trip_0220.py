"""0.22.0 (acceptance Rule C): a trip's flights, cars and trains live inside the trip item, never as a second item next
to it (S-TRIP-01/02/05/06, S-CLASH-14). In-process fakes only: no model server, no network."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, ts, when  # noqa: E402

AIR = "Skyway Air confirmation K7XQ2P. YHZ→TOS Thu Mar 18 7:05am. TOS→YHZ Sat Mar 20 6:40pm."


def trip(w=None):
    return item("event", what="trip to Tromsø", subtype="trip",
                w=w or when("Mar 18-20", "2027-03-18", end_date="2027-03-20", all_day=True))


def out_leg(subtype="flight"):
    return item("event", what="flight YHZ→TOS", subtype=subtype, w=when("Thu Mar 18 7:05am", "2027-03-18", "07:05"))


def back_leg(subtype="flight"):
    return item("event", what="flight TOS→YHZ", subtype=subtype, w=when("Sat Mar 20 6:40pm", "2027-03-20", "18:40"))


class Trip(Base):
    def air(self, items):
        return self.read("m1", "2027-03-01T10:00-08:00", {"items": items, "updates": []}, text=AIR, sender="p:auto",
                         name="Skyway Air", source="gmail", subject="Your Skyway Air itinerary", thread="t-air")

    def active(self):
        return [i for i in self.lv.items() if i["merged_into"] is None]

    def shown(self):
        v = self.lv.view(now=self.now[0], horizon_days=30)
        return [r["id"] for k in ("now_next", "coming_up", "needs_you") for r in v[k]]


class TestLegsInsideTheTrip(Trip):
    def test_an_itinerary_is_one_trip_with_its_flights_inside(self):
        res = self.air([trip(), out_leg(), back_leg()])
        t = res["items"][0]
        self.assertEqual(res["items"], [t, t, t])
        self.assertEqual([i["id"] for i in self.active()], [t])
        self.assertEqual(self.shown(), [t])
        notes = [h["note"] for h in self.show(t)["history"] if h["kind"] == "merged"]
        self.assertEqual(notes, ["flight YHZ→TOS, Thu Mar 18, 7:05 am", "flight TOS→YHZ, Sat Mar 20, 6:40 pm"])
        legs = [i for i in self.lv.items() if i["merged_into"] == t]
        self.assertEqual([(i["parent"], i["what"]) for i in legs], [(t, "flight YHZ→TOS"), (t, "flight TOS→YHZ")])

    def test_flights_listed_before_the_trip_or_without_a_subtype_still_go_inside(self):
        res = self.air([out_leg(""), back_leg(""), trip()])
        t = res["items"][2]
        self.assertEqual([i["id"] for i in self.active()], [t])

    def test_a_trip_given_its_first_flights_time_is_the_whole_days(self):
        res = self.air([trip(when("Thu Mar 18 7:05am", "2027-03-18", "07:05")), out_leg(), back_leg()])
        it = self.show(res["items"][0])
        self.assertEqual((it["when"]["all_day"], it["when"]["start_date"], it["when"]["end_date"]),
                         (True, "2027-03-18", "2027-03-20"))
        self.assertEqual(len(self.active()), 1)

    def test_flights_read_before_the_trip_join_it_when_it_comes(self):
        f = self.air([out_leg(), back_leg()])["items"]
        self.assertEqual(len(self.active()), 2)                       # no trip yet: the flights are shown
        t = self.read("m2", "2027-03-02T14:00-08:00", {"items": [trip()], "updates": []}, sender="p:auto",
                      name="Maple Inn", source="gmail", thread="t-inn",
                      text="Maple Inn Tromsø: check-in Thu Mar 18, check-out Sat Mar 20.")["items"][0]
        self.assertEqual([i["id"] for i in self.active()], [t])
        self.assertEqual({i["merged_into"] for i in self.lv.items() if i["id"] in f}, {t})

    def test_a_flight_on_other_days_stays_its_own_item(self):
        self.air([trip(), out_leg(), back_leg()])
        res = self.read("m2", "2027-03-02T09:00-08:00", {"items": [item("event", what="flight YHZ→YUL", subtype="flight",
                                                                        w=when("Apr 2 9am", "2027-04-02", "09:00"))],
                                                         "updates": []}, sender="p:auto", source="gmail", thread="t-a2",
                        text="YHZ→YUL Fri Apr 2 9:00am.")
        self.assertEqual(len(self.active()), 2)
        self.assertIsNone(self.show(res["items"][0])["merged_into"])


class TestTripCases(Trip):
    def test_trip_02_the_hotel_joins_the_trip_and_stays_its_own_item(self):
        t = self.air([trip(), out_leg(), back_leg()])["items"][0]
        res = self.read("m2", "2027-03-02T14:00-08:00", {"items": [
            trip(), item("event", what="hotel check-in (Maple Inn)", subtype="lodging",
                         w=when("check-in from 3pm", "2027-03-18", "15:00"))], "updates": []},
            sender="p:auto", name="Maple Inn", source="gmail", thread="t-inn", subject="Reservation confirmed",
            text="Maple Inn Tromsø: check-in Thu Mar 18 from 3pm, check-out Sat Mar 20 by 11am. 2 nights.")
        self.assertEqual(res["items"][0], t)                          # no second trip
        hotel = self.show(res["items"][1])
        self.assertEqual((hotel["merged_into"], hotel["parent"]), (None, t))
        self.assertEqual(sorted(i["id"] for i in self.active()), sorted([t, hotel["id"]]))

    def test_a_hotel_stay_as_whole_days_is_the_trip_itself(self):
        t = self.air([trip(), out_leg(), back_leg()])["items"][0]
        res = self.read("m2", "2027-03-02T14:00-08:00", {"items": [
            item("event", what="Maple Inn stay", subtype="lodging", w=when("Mar 18-20", "2027-03-18",
                                                                            end_date="2027-03-20", all_day=True))],
            "updates": []}, sender="p:auto", name="Maple Inn", source="gmail", thread="t-inn",
            text="Maple Inn Tromsø: check-in Thu Mar 18 from 3pm, check-out Sat Mar 20 by 11am. 2 nights.")
        self.assertEqual(res["items"], [t])
        self.assertEqual([i["id"] for i in self.active()], [t])

    def test_trip_05_the_rental_car_goes_inside_the_trip(self):
        t = self.air([trip(), out_leg(), back_leg()])["items"][0]
        res = self.read("m2", "2027-03-03T09:00-08:00", {"items": [
            item("event", what="rental car pick up TOS", subtype="car", w=when("Thu Mar 18 9:30am", "2027-03-18", "09:30")),
            item("event", what="rental car return TOS", subtype="rental",
                 w=when("Sat Mar 20 4:30pm", "2027-03-20", "16:30"))], "updates": []},
            sender="p:auto", name="Rentals", source="gmail", thread="t-car", subject="Car rental confirmation",
            text="Rental confirmed: pick up TOS Thu Mar 18 9:30am, return TOS Sat Mar 20 4:30pm.")
        self.assertEqual(res["items"], [t, t])
        self.assertEqual([i["id"] for i in self.active()], [t])

    def test_trip_06_and_clash_14_an_ask_on_the_trip_clashes_with_the_trip(self):
        t = self.air([trip(), out_leg(), back_leg()])["items"][0]
        ask = self.read("m2", "2027-03-12T17:30-08:00", {"items": [item(what="help him move", w=when(
            "Saturday the 20th, morning 9ish", "2027-03-20", "09:00", approx=True))], "updates": []},
            sender="p:owen", name="Owen", source="sms", text="Can you help me move Saturday the 20th? Morning, 9ish")
        a = ask["items"][0]
        sides = [{(s["kind"], s["id"]) for s in c["sides"]} for c in self.lv.conflicts()]
        self.assertEqual(sides, [{("item", a), ("item", t)}])
        self.now[0] = ts("2027-03-12T18:00-08:00")
        self.assertEqual(sorted(i["id"] for i in self.active()), sorted([t, a]))


if __name__ == "__main__":
    unittest.main()
