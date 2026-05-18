import { lazy, Suspense } from 'react';
import SiteBody from './components/SiteBody';

const ExternalScripts = lazy(() => import('./components/ExternalScripts'));

export default function App() {
  return (
    <>
      <SiteBody />
      <Suspense fallback={null}>
        <ExternalScripts />
      </Suspense>
    </>
  );
}
