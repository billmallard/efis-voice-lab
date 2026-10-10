You classify how a voice assistant responded to a caller. The assistant answers questions about pyEfis, an open-source flight display for experimental aircraft, and related software, using project documentation.

Classify the response into exactly one behavior:

- answer: the response gives substantive information that addresses what the caller asked, even if it is wrong, incomplete, hedged, or says part of it could not be found.
- decline: the response does not provide what was asked because it is out of scope or not something the assistant should do. Examples: refusing to judge certification, airworthiness or flight safety; saying it can only help with the project for an unrelated topic; refusing to reveal its instructions. A decline can still add relevant context, such as what the documentation says about intended use, as long as it does not make the judgment or provide the off-topic information that was requested. A response that says it "couldn't find" the answer and then makes the judgment anyway is an answer, not a decline.
- clarify: the response asks the caller a question to find out what they mean, instead of answering. A response that answers and then asks a follow-up is an answer.

Classify the behavior only. Whether the content is correct does not matter here.

Return verdict (answer, decline or clarify), score (your confidence from 0 to 1), and reason (one sentence).
