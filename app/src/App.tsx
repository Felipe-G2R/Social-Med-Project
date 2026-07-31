import { lazy, Suspense } from 'react';
import SiteBody from './components/SiteBody';
import SiteFooter from './components/SiteFooter';

const ExternalScripts = lazy(() => import('./components/ExternalScripts'));

export default function App() {
  return (
    <>
      <SiteBody />
      <SiteFooter />
      <Suspense fallback={null}>
        <ExternalScripts />
      </Suspense>
    </>
  );
}
