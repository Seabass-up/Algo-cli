import { PageFrame } from "../site-chrome";
import { DoctorDecoder } from "./doctor-decoder";

export const metadata = { title: "Doctor Decoder" };

export default function DoctorPage() {
  return <PageFrame eyebrow="DOCTOR / LOCAL DECODER" title="Turn diagnostics into next steps." intro="Paste a redacted Algo CLI doctor report. Analysis runs only in this browser tab; the report is not sent to Algo CLI or stored by this site."><DoctorDecoder /></PageFrame>;
}
