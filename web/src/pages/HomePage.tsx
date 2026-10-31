// The front door: what GaugeLens does, how it works and why its readings can be trusted, with a way straight in.
import { Link } from "react-router-dom";
import { useAuth } from "../auth";
import { Icon } from "../icons";

const STEPS = [
  { icon: "scan", title: "Capture", text: "Record the displays with your phone, or take a photo. Nothing to install." },
  { icon: "readings", title: "Read", text: "SAM 3.1 finds each display and Muse Spark reads the visible values, identifying the equipment from its nameplate." },
  { icon: "physics", title: "Verify", text: "Values are checked against physics. When one can't be right, GaugeLens looks again and corrects the misread if the image supports it." },
  { icon: "chat", title: "Explain", text: "Confirmed faults get a diagnosis, and you can ask about anything you filmed. Answers cite the evidence." },
  { icon: "check", title: "Decide", text: "GaugeLens proposes one safe next check. A person approves or rejects it, and the decision is recorded." },
] as const;

const REASONS = [
  { icon: "physics", title: "Checked against physics", text: "Power, voltage, current and running totals have to agree. A reading that breaks those rules isn't simply accepted." },
  { icon: "frame", title: "Evidence you can open", text: "Every citation links to the frame and reading behind it. An answer that can't be verified is marked as such." },
  { icon: "guardrail", title: "A person decides", text: "GaugeLens never acts on equipment. It proposes one safe next check, and a person approves or rejects it." },
  { icon: "lock", title: "Private by default", text: "A guest's scans and chat are deleted when the session ends. Create an account only if you want to keep them." },
] as const;

export default function HomePage() {
  const { me } = useAuth();
  return (
    <div className="home">
      <section className="home-hero">
        <div>
          <h1>Verified readings from any digital display</h1>
          <p className="home-lead">
            Point your phone at an inverter, meter or generator display. GaugeLens reads the values it can see, checks them
            against physics, looks again when they can't be right and explains what it finds.
          </p>
          <div className="row home-actions">
            <Link className="btn primary" to="/scan"><Icon name="scan" size={15} /> Start a scan</Link>
            <Link className="btn" to="/upload"><Icon name="upload" size={15} /> Upload a video or photos</Link>
          </div>
          <p className="muted small">{me?.kind === "guest" ? "No account needed." : <>Your results are under <Link to="/incidents">Incidents</Link> and <Link to="/clips">Clips</Link>.</>}</p>
        </div>
        <figure className="home-example" aria-label="Example of a misread being corrected">
          <figcaption className="muted small">Example from a test panel: a misread caught by physics</figcaption>
          <div className="example-row"><span className="muted">First read</span><span className="mono example-wrong">838 W</span></div>
          <div className="example-row"><span className="muted">Same screen</span><span className="mono">393 VA</span></div>
          <p className="example-why small"><Icon name="physics" size={13} /> Real power can't exceed apparent power, so GaugeLens looks at the digits again.</p>
          <div className="example-row"><span className="muted">Re-look</span><span className="mono example-right"><Icon name="check" size={13} /> 338 W</span></div>
        </figure>
      </section>

      <section aria-labelledby="how">
        <h2 id="how">How it works</h2>
        <ol className="home-steps">
          {STEPS.map((s) => (
            <li key={s.title} className="card">
              <Icon name={s.icon} size={20} />
              <h3>{s.title}</h3>
              <p>{s.text}</p>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="why">
        <h2 id="why">Why you can rely on it</h2>
        <div className="home-reasons">
          {REASONS.map((r) => (
            <div key={r.title} className="home-reason">
              <Icon name={r.icon} size={18} />
              <div><h3>{r.title}</h3><p>{r.text}</p></div>
            </div>
          ))}
        </div>
      </section>

      <p className="notice safety small">
        Physics catches errors that break the rules, such as a wrong digit or a dropped decimal, not every small misread. Stay outside
        enclosures, film only what's visible from the front panel, and leave electrical work to a qualified technician.
      </p>
    </div>
  );
}
