import { useOtp } from './useOtp.js'
import Landing from './Landing.jsx'
import Phone from './Phone.jsx'

export default function App() {
  const otp = useOtp()
  const open = otp.phase !== 'idle'

  return (
    <>
      <Landing onConnect={otp.start} />
      <Phone
        open={open}
        mc={otp.mc}
        phase={otp.phase}
        code={otp.code}
        secsLeft={otp.secsLeft}
        ttl={otp.ttl}
        onClose={otp.stop}
      />
    </>
  )
}
