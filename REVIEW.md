# REVIEW.md

## Bugs fixed

1. **Duplicate punch-in:** Added a unique index on `emp_code` and `date` and handled `DuplicateKeyError` with HTTP 409.
2. **Attendance listing performance:** Removed an unnecessary extra database query for each attendance record by using the common `record_out()` serializer.
3. **Timestamp response format:** Normalized punch timestamps and audit-history timestamps to epoch milliseconds in API responses.
4. **Duplicate punch-out protection:** Added a conditional update so a record cannot be successfully punched out twice.
5. **Input validation:** Added validation for employee codes, email addresses, shift times, month formats, and pagination limits.

## Verification

- Tested duplicate punch-in rejection.
- Tested attendance listing and pagination response.
- Tested all five explain endpoints on the sample dataset.
- Verified that the API starts on port 8000.

## Remaining considerations

- Test overnight-shift punch-out behavior and all business-rule boundary cases.
- Run the complete API test suite against a larger dataset before submission.
