/* demo-data.js — SAMPLE data for the public demo (/demo).
 *
 * Everything here is fictional: session titles, codes, rooms and speaker names
 * are made up for illustration and are NOT the real re:Invent catalog. The demo
 * never calls AWS, AWS Events, or any backend.
 *
 * Field names mirror what the real backend returns (see backend/models.py
 * Session.to_public_dict): sessionId, sessionCode, title, type, level, room,
 * venue, services[], topics[], features[], speakers[] (names), isReservable,
 * isAllDay, abstract, sessionTime{date,time,length}. `time` is 24h Pacific.
 */
(function () {
  "use strict";

  function s(code, title, type, level, venue, room, date, time, length,
             services, topics, speakers, isReservable, abstract) {
    return {
      sessionId: "demo-" + code.toLowerCase(),
      sessionCode: code,
      title: title,
      type: type,
      level: level,
      room: room,
      venue: venue,
      services: services,
      topics: topics,
      features: [],
      speakers: speakers,
      isReservable: isReservable,
      isAllDay: false,
      abstract: abstract,
      sessionTime: { date: date, time: time, length: length },
    };
  }

  var L100 = "100 - Foundational";
  var L200 = "200 - Intermediate";
  var L300 = "300 - Advanced";
  var L400 = "400 - Expert";

  var VEN = "Venetian";
  var WYN = "Wynn/Encore";
  var MGM = "MGM Grand";
  var CAE = "Caesars Forum";
  var MAN = "Mandalay Bay";

  var sessions = [
    s("KEY001", "Keynote: The innovation keynote (sample)", "Keynote", L100, VEN,
      "Level 2, Venetian Ballroom", "2026-12-01", "08:00", 120,
      [], ["Keynote"], ["Sample Speaker"], false,
      "A sample keynote entry. Keynotes are walk-up only, so there is no Reserve button on this card."),

    s("AIM301", "Building production agents with Amazon Bedrock AgentCore", "Breakout session", L300, VEN,
      "Level 3, Titian 2205", "2026-12-01", "10:30", 60,
      ["Amazon Bedrock AgentCore", "Amazon Bedrock"], ["Generative AI", "Agentic AI"],
      ["Alex Rivera", "Priya Nair"], true,
      "Go from a prototype agent to a production service. This sample session covers tool use, memory, identity, and observability, with a live walkthrough of deploying an agent behind an API."),

    s("AIM205", "Getting started with Amazon Bedrock: your first generative AI app", "Breakout session", L200, VEN,
      "Level 2, Titian 2201", "2026-12-01", "13:00", 60,
      ["Amazon Bedrock"], ["Generative AI"],
      ["Maya Chen", "Luis Ortega"], true,
      "A beginner-friendly tour of building a chat app with foundation models: choosing a model, writing prompts, adding guardrails, and keeping costs predictable."),

    s("SVS201", "Serverless fundamentals: Lambda, API Gateway and EventBridge", "Breakout session", L200, MGM,
      "Level 1, Room 108", "2026-12-01", "15:00", 60,
      ["AWS Lambda", "Amazon API Gateway", "Amazon EventBridge"], ["Serverless"],
      ["Elena Petrova", "Marcus Bell"], true,
      "Learn the building blocks of serverless applications and how they fit together. Ideal if you are new to event-driven design."),

    s("AIM215", "Prompt engineering for builders: a beginner-friendly workshop", "Workshop", L200, MAN,
      "Level 2, South Pacific D", "2026-11-30", "14:00", 120,
      ["Amazon Bedrock"], ["Generative AI"],
      ["Chris Lee"], true,
      "Hands-on practice writing, testing, and evaluating prompts. Bring a laptop; no prior machine learning experience required."),

    s("DVT215", "Hands-on infrastructure as code with the AWS CDK", "Workshop", L200, MAN,
      "Level 3, Islander F", "2026-11-30", "10:00", 120,
      ["AWS CDK"], ["Developer Tools", "Architecture"],
      ["Jun Park", "Rosa Alvarez"], true,
      "Define a serverless stack in code, deploy it, and refactor it into reusable constructs."),

    s("DVT201", "Meet Kiro: spec-driven development in your IDE", "Breakout session", L200, VEN,
      "Level 4, Lando 4301", "2026-12-02", "09:00", 60,
      ["Kiro"], ["Developer Tools", "Agentic AI"],
      ["Riley Morgan", "Aiko Tanaka"], true,
      "See how requirements, design, and tasks turn into working code with an agentic IDE. A sample session about spec-driven development."),

    s("DVT310", "Chalk talk: Specs and steering for large codebases with Kiro", "Chalk talk", L300, WYN,
      "Level 1, Latour 2", "2026-12-02", "11:30", 60,
      ["Kiro"], ["Developer Tools"],
      ["Riley Morgan"], true,
      "An interactive discussion on structuring specs, steering files, and hooks so an agent stays aligned with your team conventions."),

    s("SEC303", "Securing generative AI workloads end to end", "Breakout session", L300, MAN,
      "Level 2, South Seas B", "2026-12-02", "14:00", 60,
      ["Amazon Bedrock", "AWS IAM"], ["Security", "Generative AI"],
      ["Dana Whitfield"], true,
      "Threat modeling, least-privilege access, data protection, and guardrails for applications built on foundation models."),

    s("AIM340", "Workshop: Red-team your AI agent before you launch", "Workshop", L300, WYN,
      "Level 1, Brahms 4", "2026-12-02", "15:00", 120,
      ["Amazon Bedrock", "AWS Lambda"], ["Generative AI", "Security"],
      ["Dana Whitfield", "Sam Okafor"], true,
      "Probe a sample agent for prompt injection, data leakage, and unsafe tool use, then harden it with guardrails and evaluation tests."),

    s("DVT320", "Observability for AI agents with CloudWatch and OpenTelemetry", "Breakout session", L300, CAE,
      "Summit 232", "2026-12-02", "16:30", 60,
      ["Amazon CloudWatch", "AWS X-Ray"], ["Developer Tools", "Generative AI"],
      ["Hana Kim"], true,
      "Trace agent steps, tool calls, and token usage end to end, and build dashboards and alarms around them."),

    s("LDR101", "Leadership session: Building an AI-ready organization", "Leadership session", L100, WYN,
      "Level 1, Lafite 3", "2026-12-04", "14:00", 60,
      [], ["Leadership"], ["Morgan Ellis"], false,
      "A walk-up leadership session on skills, governance, and culture for AI adoption. Walk-up only, so no Reserve button."),

    s("AIM350", "Builders' session: Multi-agent orchestration", "Builders' session", L300, VEN,
      "Level 2, Palazzo K", "2026-12-03", "09:00", 60,
      ["Amazon Bedrock AgentCore"], ["Agentic AI"],
      ["Jordan Blake"], true,
      "Build a small team of cooperating agents. This sample session is marked full in the demo so you can see how a full session is reported."),

    s("AIM410", "Workshop: Fine-tune and deploy a custom model on Amazon SageMaker", "Workshop", L400, MGM,
      "Level 1, Room 105", "2026-12-03", "08:30", 120,
      ["Amazon SageMaker AI"], ["Machine Learning"],
      ["Nia Patel", "Tomas Silva"], true,
      "Prepare a dataset, fine-tune a model, evaluate it, and deploy it to an endpoint."),

    s("SVS320", "Code talk: Event-driven architectures that scale to zero", "Code talk", L300, VEN,
      "Level 3, Titian 2208", "2026-12-03", "10:00", 60,
      ["AWS Lambda", "Amazon EventBridge", "Amazon SQS"], ["Serverless", "Architecture"],
      ["Marcus Bell"], true,
      "Live coding of an event-driven system that costs nothing when idle: routing, retries, dead-letter queues, and idempotency."),

    s("DAT301", "Vector search and RAG on Amazon Aurora and Amazon OpenSearch Service", "Breakout session", L300, VEN,
      "Level 3, Titian 2203", "2026-12-03", "11:30", 60,
      ["Amazon Aurora", "Amazon OpenSearch Service"], ["Data & Analytics", "Generative AI"],
      ["Ibrahim Yusuf"], true,
      "Compare vector search options, chunking strategies, and retrieval quality for retrieval-augmented generation."),

    s("SVS101", "Builders' session: Ship your first AWS Lambda function", "Builders' session", L100, MGM,
      "Level 3, Room 321", "2026-12-03", "13:30", 60,
      ["AWS Lambda"], ["Serverless"],
      ["Omar Haddad"], true,
      "A guided, beginner-friendly build: write, deploy, and test a function in under an hour."),

    s("ARC302", "Designing multi-region resilient architectures", "Breakout session", L300, CAE,
      "Summit 220", "2026-12-03", "14:00", 60,
      [], ["Architecture"],
      ["Kenji Watanabe", "Sofia Ramirez"], true,
      "Patterns for failover, data replication, and testing recovery objectives across regions."),

    s("STG211", "Storage choices for AI and ML data lakes", "Breakout session", L200, MGM,
      "Level 1, Room 110", "2026-12-04", "09:30", 60,
      ["Amazon S3"], ["Storage", "Machine Learning"],
      ["Grace Liu"], true,
      "How to organize, secure, and cost-optimize the data behind training and inference pipelines."),

    s("SVS402", "Chalk talk: Advanced AWS Step Functions patterns for agent workflows", "Chalk talk", L400, WYN,
      "Level 1, Mozart 1", "2026-12-04", "11:00", 60,
      ["AWS Step Functions"], ["Serverless", "Agentic AI"],
      ["Elena Petrova"], true,
      "Orchestrating long-running, human-in-the-loop agent workflows with state machines."),
  ];

  // Codes that report "session full" when you try to reserve them.
  var fullSessionCodes = ["AIM350"];

  // Starting schedule (what "My Schedule" shows before you change anything).
  // Personal-time datetimes use the real contract: UTC, no Z, seconds 00.
  var initial = {
    favorites: ["demo-aim301", "demo-dvt201", "demo-svs320"],
    reserved: ["demo-aim340", "demo-aim410"],
    personalTime: [
      { personalTimeId: "demo-pt-1", title: "Badge pickup and registration",
        start: "2026-12-01T15:00:00", end: "2026-12-01T16:00:00",
        location: "Venetian Expo" },                                     // 7:00-8:00 AM PST
      { personalTimeId: "demo-pt-2", title: "Lunch with the team",
        start: "2026-12-02T20:00:00", end: "2026-12-02T21:00:00",
        description: "Pick a spot near the Venetian.",
        location: "The Venetian food court" },                           // 12:00-1:00 PM PST
      { personalTimeId: "demo-pt-3", title: "Expo floor walk",
        start: "2026-12-04T00:00:00", end: "2026-12-04T01:30:00" },       // Dec 3, 4:00-5:30 PM PST
    ],
  };

  window.DEMO_DATA = {
    sessions: sessions,
    fullSessionCodes: fullSessionCodes,
    initial: initial,
  };
})();
